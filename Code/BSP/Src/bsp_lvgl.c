#include "bsp_lvgl.h"
#include "main.h"
#include "bsp_board.h"
#include "lcd.h"
#include "cmsis_os.h"

#include <stdint.h>

/* hrtc is defined by CubeMX in Core/Src/main.c. */
extern RTC_HandleTypeDef hrtc;

/* lcd.c finishes LCD_Init() with LCD_Display_Dir(0): SSD1963 portrait
   coordinate space is 480 pixels wide by 800 pixels high. */
#define LVGL_HOR_RES       480U
#define LVGL_VER_RES       800U
#define LVGL_BUF_LINES     10U
#define CLOCK_DIAL_SIZE    320
#define CLOCK_DIAL_CENTER  (CLOCK_DIAL_SIZE / 2)
#define ADC_SCOPE_WIDTH    410
#define ADC_SCOPE_HEIGHT   230

/* Normal runtime configuration.  Keep these switches only as reversible
   diagnostics; all display, UI and timer paths are enabled. */
#define LVGL_DIAG_SKIP_LCD_FLUSH 0U
#define LVGL_DIAG_SKIP_TIMER_HANDLER 0U
#define LVGL_DIAG_INIT_STAGE 3U
#define LVGL_DIAG_UI_STAGE 3U

static lv_color_t lvgl_draw_buf_mem[LVGL_HOR_RES * LVGL_BUF_LINES];
static lv_disp_draw_buf_t lvgl_draw_buf;
/* LVGL saves pointers to these driver descriptors.  They must therefore
   outlive BSP_LVGL_Init()/ui_create(); automatic locals cause a later
   refresh/input callback to jump through overwritten stack memory. */
static lv_disp_drv_t lvgl_disp_drv;
static lv_indev_drv_t lvgl_indev_drv;
static lv_group_t *ui_group;
static lv_indev_t *keypad_indev;
static volatile uint8_t lvgl_started;

static lv_obj_t *main_screen;
static lv_obj_t *main_clock_label;
static lv_obj_t *main_menu_buttons[5];
static lv_obj_t *clock_face;
static lv_obj_t *clock_hour_hand;
static lv_obj_t *clock_minute_hand;
static lv_obj_t *clock_second_hand;
static lv_obj_t *settings_screen;
static lv_obj_t *settings_fields[6];
static lv_obj_t *settings_values[6];
static lv_obj_t *dac_screen;
static lv_obj_t *dac_fields[5];
static lv_obj_t *dac_values[5];
static lv_obj_t *dac_frequency_digits[5];
static lv_obj_t *dac_frequency_dot;
static lv_obj_t *dac_frequency_unit;
static lv_obj_t *dac_frequency_box;
static lv_obj_t *adc_screen;
static lv_obj_t *adc_frequency_label;
static lv_obj_t *adc_vpp_label;
static lv_obj_t *adc_wave_label;
static lv_obj_t *adc_scope_panel;
static lv_obj_t *adc_scope_line;
static lv_obj_t *adc_back_button;
static lv_obj_t *alarm_screen;
static lv_obj_t *alarm_menu_buttons[3];
static lv_obj_t *alarm_menu_values[3];
static lv_obj_t *alarm_chime_indicator;
static lv_obj_t *alarm_edit_screen;
static lv_obj_t *alarm_edit_title;
static lv_obj_t *alarm_edit_fields[4];
static lv_obj_t *alarm_edit_values[4];
static lv_obj_t *alarm_edit_indicator;
static lv_obj_t *alarm_popup_screen;
static lv_obj_t *alarm_popup_title;
static lv_obj_t *alarm_popup_hint;
static lv_obj_t *alarm_popup_close_button;

static RTC_TimeTypeDef ui_time;
static RTC_DateTypeDef ui_date;
static uint8_t ui_field;
static uint8_t ui_editing;
static uint8_t ui_dac_field;
static uint8_t ui_dac_editing;
static uint8_t ui_dac_selecting_frequency_digit;
static uint8_t ui_dac_frequency_digit = 1U;
static uint8_t ui_page;
static uint32_t ui_last_clock_stamp = 0xFFFFFFFFU;
static BspAdcScopeFrame ui_adc_scope_frame;
static BspAlarmConfig ui_alarm_config;
static uint8_t ui_alarm_id;
static uint8_t ui_alarm_field;
static uint8_t ui_alarm_editing;
static uint8_t ui_time_chime_enabled;
static uint8_t ui_alarm_ringing;
static uint8_t ui_active_alarm_id;
static uint8_t ui_page_before_alarm;
static uint8_t ui_buzzer_phase;
static uint32_t ui_buzzer_deadline;
static uint32_t ui_alarm_started_tick;

static BspDacConfig ui_dac_config = {
    BSP_DAC_WAVE_SINE,
    33U,     /* 3.3 Vpp maximum; the user can reduce it in 0.1 V steps. */
    50U,
    1000U,  /* 100.0 Hz */
    0U      /* Start paused for safety. */
};

#define UI_PAGE_MAIN      0U
#define UI_PAGE_SETTINGS  1U
#define UI_PAGE_DAC       2U
#define UI_PAGE_ADC       3U
#define UI_PAGE_ALARM_MENU 4U
#define UI_PAGE_ALARM_EDIT 5U
#define UI_PAGE_ALARM_POPUP 6U

/* Main and the emergency alarm popup are permanent.  These ordinary pages
   share a three-entry least-recently-used cache. */
#define UI_PAGE_CACHE_CAPACITY 3U
#define UI_CACHE_SETTINGS      0U
#define UI_CACHE_DAC           1U
#define UI_CACHE_ADC           2U
#define UI_CACHE_ALARM_MENU    3U
#define UI_CACHE_ALARM_EDIT    4U
#define UI_CACHE_SLOT_COUNT    5U

static uint32_t ui_page_cache_stamp[UI_CACHE_SLOT_COUNT];
static uint32_t ui_page_cache_clock;

/* Frequency is stored in 0.1 Hz units.  The default selected digit is the
   ones position (10 tenths = 1 Hz), rather than the inconvenient 0.1 Hz. */
#define UI_DAC_FREQ_DIGIT_TENTHS     0U
#define UI_DAC_FREQ_DIGIT_ONES       1U
#define UI_DAC_FREQ_DIGIT_TENS       2U
#define UI_DAC_FREQ_DIGIT_HUNDREDS   3U
#define UI_DAC_FREQ_DIGIT_THOUSANDS  4U

/* lv_line_set_points() stores the point-array address, so these must have
   static lifetime just like the LVGL driver descriptors. */
static lv_point_t clock_hour_points[2];
static lv_point_t clock_minute_points[2];
static lv_point_t clock_second_points[2];
static lv_point_t adc_scope_points[BSP_ADC_SCOPE_POINTS];

/* sin(0..354 degrees in 6 degree steps), scaled by 1000. */
static const int16_t clock_sin_60[60] = {
       0,  105,  208,  309,  407,  500,  588,  669,  743,  809,
     866,  914,  951,  978,  995, 1000,  995,  978,  951,  914,
     866,  809,  743,  669,  588,  500,  407,  309,  208,  105,
       0, -105, -208, -309, -407, -500, -588, -669, -743, -809,
    -866, -914, -951, -978, -995,-1000, -995, -978, -951, -914,
    -866, -809, -743, -669, -588, -500, -407, -309, -208, -105
};

static void lvgl_flush_cb(lv_disp_drv_t *disp_drv,
                          const lv_area_t *area,
                          lv_color_t *color_p)
{
#if LVGL_DIAG_SKIP_LCD_FLUSH
    (void)area;
    (void)color_p;
    lv_disp_flush_ready(disp_drv);
#else
    uint16_t width = (uint16_t)(area->x2 - area->x1 + 1);
    uint16_t height = (uint16_t)(area->y2 - area->y1 + 1);
    uint32_t count = (uint32_t)width * height;

    LCD_Set_Window(area->x1, area->y1, width, height);
    LCD_WriteRAM_Prepare();

    while (count-- > 0U)
    {
        LCD_WriteRAM(color_p->full);
        color_p++;
    }

    lv_disp_flush_ready(disp_drv);
#endif
}

static uint8_t ui_is_leap_year(uint16_t year)
{
    return (uint8_t)(((year % 4U) == 0U && (year % 100U) != 0U) ||
                     ((year % 400U) == 0U));
}

static uint8_t ui_days_in_month(uint8_t year, uint8_t month)
{
    static const uint8_t days[] =
        {31U, 28U, 31U, 30U, 31U, 30U, 31U, 31U, 30U, 31U, 30U, 31U};
    uint16_t full_year = (uint16_t)(2000U + year);

    if (month == 2U && ui_is_leap_year(full_year) != 0U) return 29U;
    if (month < 1U || month > 12U) return 31U;
    return days[month - 1U];
}

static uint8_t ui_weekday(uint8_t year, uint8_t month, uint8_t date)
{
    uint32_t days = 0U;
    uint16_t full_year = (uint16_t)(2000U + year);

    for (uint16_t y = 2000U; y < full_year; y++)
    {
        days += (ui_is_leap_year(y) != 0U) ? 366U : 365U;
    }

    for (uint8_t m = 1U; m < month; m++)
    {
        days += ui_days_in_month(year, m);
    }

    days += (uint32_t)date - 1U;

    /* 2000-01-01 was Saturday; RTC uses Monday=1 ... Sunday=7. */
    return (uint8_t)(((days + 5U) % 7U) + 1U);
}

static void ui_read_rtc(void)
{
    (void)HAL_RTC_GetTime(&hrtc, &ui_time, RTC_FORMAT_BIN);
    (void)HAL_RTC_GetDate(&hrtc, &ui_date, RTC_FORMAT_BIN);
}

static void ui_write_rtc(void)
{
    RTC_TimeTypeDef time = {0};
    RTC_DateTypeDef date = {0};

    time.Hours = ui_time.Hours;
    time.Minutes = ui_time.Minutes;
    time.Seconds = ui_time.Seconds;
    time.TimeFormat = RTC_HOURFORMAT12_AM;
    time.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
    time.StoreOperation = RTC_STOREOPERATION_RESET;

    date.Year = ui_date.Year;
    date.Month = ui_date.Month;
    date.Date = ui_date.Date;
    date.WeekDay = ui_weekday(date.Year, date.Month, date.Date);

    (void)HAL_RTC_SetTime(&hrtc, &time, RTC_FORMAT_BIN);
    (void)HAL_RTC_SetDate(&hrtc, &date, RTC_FORMAT_BIN);
}

static void ui_update_settings_text(void)
{
    lv_label_set_text_fmt(settings_values[0], "Hour   %02u", ui_time.Hours);
    lv_label_set_text_fmt(settings_values[1], "Minute %02u", ui_time.Minutes);
    lv_label_set_text_fmt(settings_values[2], "Second %02u", ui_time.Seconds);
    lv_label_set_text_fmt(settings_values[3], "Year   20%02u", ui_date.Year);
    lv_label_set_text_fmt(settings_values[4], "Month  %02u", ui_date.Month);
    lv_label_set_text_fmt(settings_values[5], "Date   %02u", ui_date.Date);
}

static const char *ui_dac_wave_name(BspDacWaveform waveform)
{
    switch (waveform)
    {
        case BSP_DAC_WAVE_SQUARE:   return "Square";
        case BSP_DAC_WAVE_TRIANGLE: return "Triangle";
        case BSP_DAC_WAVE_SAWTOOTH: return "Sawtooth";
        case BSP_DAC_WAVE_SINE:
        default:                    return "Sine";
    }
}

static uint16_t ui_dac_frequency_step_x10(void)
{
    static const uint16_t steps[5] = {1U, 10U, 100U, 1000U, 10000U};

    if (ui_dac_frequency_digit > UI_DAC_FREQ_DIGIT_THOUSANDS)
        ui_dac_frequency_digit = UI_DAC_FREQ_DIGIT_ONES;
    return steps[ui_dac_frequency_digit];
}

static void ui_update_dac_frequency_display(void)
{
    uint16_t integer_part = (uint16_t)(ui_dac_config.frequency_x10 / 10U);
    uint8_t digits[5];

    digits[0] = (uint8_t)((integer_part / 1000U) % 10U);
    digits[1] = (uint8_t)((integer_part / 100U) % 10U);
    digits[2] = (uint8_t)((integer_part / 10U) % 10U);
    digits[3] = (uint8_t)(integer_part % 10U);
    digits[4] = (uint8_t)(ui_dac_config.frequency_x10 % 10U);

    for (uint8_t i = 0U; i < 5U; i++)
        lv_label_set_text_fmt(dac_frequency_digits[i], "%u", digits[i]);

    if ((ui_dac_selecting_frequency_digit != 0U) ||
        (ui_dac_editing != 0U && ui_dac_field == 3U))
    {
        if (ui_dac_frequency_digit > UI_DAC_FREQ_DIGIT_THOUSANDS)
            ui_dac_frequency_digit = UI_DAC_FREQ_DIGIT_ONES;

        /* Align to the selected digit object itself, not a guessed pixel
           coordinate.  This keeps the smaller box exactly centered on it. */
        lv_obj_align_to(dac_frequency_box,
                        dac_frequency_digits[UI_DAC_FREQ_DIGIT_THOUSANDS -
                                             ui_dac_frequency_digit],
                        LV_ALIGN_CENTER, 0, 0);
        lv_obj_clear_flag(dac_frequency_box, LV_OBJ_FLAG_HIDDEN);
    }
    else
    {
        lv_obj_add_flag(dac_frequency_box, LV_OBJ_FLAG_HIDDEN);
    }
}

static void ui_update_dac_text(void)
{
    lv_label_set_text_fmt(dac_values[0], "Waveform   %s",
                          ui_dac_wave_name(ui_dac_config.waveform));
    lv_label_set_text_fmt(dac_values[1], "Vpp        %u.%u V",
                          ui_dac_config.vpp_x10 / 10U,
                          ui_dac_config.vpp_x10 % 10U);
    lv_label_set_text_fmt(dac_values[2], "Duty       %u%%",
                          ui_dac_config.duty_percent);
    ui_update_dac_frequency_display();
    lv_label_set_text_fmt(dac_values[4], "Output     %s",
                          (ui_dac_config.running != 0U) ? "Running" : "Paused");
}

static void ui_update_dac_appearance(void)
{
    const lv_color_t normal_bg = lv_color_hex(0x172033);
    const lv_color_t normal_text = lv_color_hex(0xF0F4FA);
    const lv_color_t normal_border = lv_color_hex(0x426080);
    const lv_color_t blue = lv_color_hex(0x1677FF);

    for (uint8_t i = 0U; i < 5U; i++)
    {
        lv_obj_set_style_bg_color(dac_fields[i], normal_bg,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(dac_fields[i], normal_bg,
                                   LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(dac_fields[i], normal_border,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(dac_fields[i], 2,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(dac_fields[i], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(dac_fields[i], 5,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(dac_values[i], normal_text,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
    }

    if ((ui_dac_editing != 0U) ||
        (ui_dac_selecting_frequency_digit != 0U))
    {
        lv_obj_set_style_bg_color(dac_fields[ui_dac_field], blue,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(dac_fields[ui_dac_field], blue,
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(dac_fields[ui_dac_field], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(dac_fields[ui_dac_field], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(dac_fields[ui_dac_field], 7,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(dac_fields[ui_dac_field], 7,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(dac_values[ui_dac_field], lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
    }
}

static void ui_apply_dac_config(void)
{
    (void)BSP_DAC_ApplyConfig(&ui_dac_config);
    ui_update_dac_text();
}

static void ui_adjust_dac_value(int8_t direction)
{
    if (ui_dac_field == 0U)
    {
        if (direction > 0)
            ui_dac_config.waveform = (BspDacWaveform)
                ((ui_dac_config.waveform + 1U) % 4U);
        else
            ui_dac_config.waveform = (ui_dac_config.waveform == BSP_DAC_WAVE_SINE) ?
                BSP_DAC_WAVE_SAWTOOTH :
                (BspDacWaveform)(ui_dac_config.waveform - 1U);
    }
    else if (ui_dac_field == 1U)
    {
        if (direction > 0)
        {
            if (ui_dac_config.vpp_x10 < 33U) ui_dac_config.vpp_x10++;
        }
        else if (ui_dac_config.vpp_x10 > 0U) ui_dac_config.vpp_x10--;
    }
    else if (ui_dac_field == 2U)
    {
        if (direction > 0)
        {
            if (ui_dac_config.duty_percent < 100U) ui_dac_config.duty_percent++;
        }
        else if (ui_dac_config.duty_percent > 0U) ui_dac_config.duty_percent--;
    }
    else if (ui_dac_field == 3U)
    {
        uint16_t step_x10 = ui_dac_frequency_step_x10();

        if (direction > 0)
        {
            /* Addition on the complete fixed-point number intentionally
               carries across decimal digits: 9.0 + 1 Hz becomes 10.0 Hz. */
            if (ui_dac_config.frequency_x10 <= (uint16_t)(20000U - step_x10))
                ui_dac_config.frequency_x10 = (uint16_t)(ui_dac_config.frequency_x10 + step_x10);
            else
                ui_dac_config.frequency_x10 = 20000U;
        }
        else if (ui_dac_config.frequency_x10 >= step_x10)
        {
            ui_dac_config.frequency_x10 = (uint16_t)(ui_dac_config.frequency_x10 - step_x10);
        }
        else
        {
            ui_dac_config.frequency_x10 = 0U;
        }
    }

    if (ui_dac_field < 4U) ui_apply_dac_config();
}

static void ui_select_dac_frequency_digit(int8_t direction)
{
    if (direction > 0)
    {
        ui_dac_frequency_digit = (uint8_t)
            ((ui_dac_frequency_digit + 1U) % 5U);
    }
    else
    {
        ui_dac_frequency_digit = (ui_dac_frequency_digit == 0U) ? 4U :
            (uint8_t)(ui_dac_frequency_digit - 1U);
    }
    ui_update_dac_text();
}

static const char *ui_adc_waveform_name(BspAdcWaveform waveform)
{
    switch (waveform)
    {
        case BSP_ADC_WAVE_SINE:     return "Sine";
        case BSP_ADC_WAVE_SQUARE:   return "Square";
        case BSP_ADC_WAVE_TRIANGLE: return "Triangle";
        case BSP_ADC_WAVE_SAWTOOTH: return "Sawtooth";
        case BSP_ADC_WAVE_UNKNOWN:
        default:                    return "Unknown";
    }
}

static void ui_update_adc_scope(void)
{
    uint16_t trace_minimum = 4095U;
    uint16_t trace_maximum = 0U;
    uint16_t display_minimum;
    uint16_t display_maximum;
    uint32_t raw_span;
    uint32_t span;
    uint32_t margin;
    uint32_t extra_margin;

    if (BSP_ADC_GetScopeFrame(&ui_adc_scope_frame) == 0U)
    {
        lv_label_set_text(adc_frequency_label, "Freq: sampling...");
        lv_label_set_text(adc_vpp_label, "Vpp: --");
        lv_label_set_text(adc_wave_label, "Wave: --");
        return;
    }

    if (ui_adc_scope_frame.frequency_x10 != 0U)
    {
        lv_label_set_text_fmt(adc_frequency_label, "Freq: %lu.%lu Hz",
                              (unsigned long)(ui_adc_scope_frame.frequency_x10 / 10U),
                              (unsigned long)(ui_adc_scope_frame.frequency_x10 % 10U));
    }
    else
    {
        lv_label_set_text(adc_frequency_label, "Freq: --");
    }

    lv_label_set_text_fmt(adc_vpp_label, "Vpp: %u.%02u V",
                          ui_adc_scope_frame.vpp_mv / 1000U,
                          (ui_adc_scope_frame.vpp_mv % 1000U) / 10U);

    if (ui_adc_scope_frame.waveform == BSP_ADC_WAVE_SQUARE)
    {
        lv_label_set_text_fmt(adc_wave_label, "Wave: Square   Duty: %u.%u%%",
                              ui_adc_scope_frame.duty_x10 / 10U,
                              ui_adc_scope_frame.duty_x10 % 10U);
    }
    else
    {
        lv_label_set_text_fmt(adc_wave_label, "Wave: %s",
                              ui_adc_waveform_name(ui_adc_scope_frame.waveform));
    }

    /* AUTO vertical scale uses only the samples that are visible on screen,
       adds 10% headroom, and leaves a fixed top/bottom margin. */
    for (uint16_t i = 0U; i < BSP_ADC_SCOPE_POINTS; i++)
    {
        if (ui_adc_scope_frame.samples[i] < trace_minimum)
            trace_minimum = ui_adc_scope_frame.samples[i];
        if (ui_adc_scope_frame.samples[i] > trace_maximum)
            trace_maximum = ui_adc_scope_frame.samples[i];
    }

    raw_span = (uint32_t)trace_maximum - trace_minimum;
    span = raw_span;
    /* Do not let AUTO magnify a small idle-input/noise span to full screen.
       A 100-code minimum is about 80 mV at the ADC input; normal DAC waves
       above 0.1 Vpp still use the complete available height. */
    if (span < 100U) span = 100U;
    margin = span / 10U + 2U;
    extra_margin = (span - raw_span) / 2U;
    display_minimum = (trace_minimum > margin + extra_margin) ?
                      (uint16_t)(trace_minimum - margin - extra_margin) : 0U;
    display_maximum = ((uint32_t)trace_maximum + margin + extra_margin < 4095U) ?
                      (uint16_t)(trace_maximum + margin + extra_margin) : 4095U;
    span = (uint32_t)display_maximum - display_minimum;

    for (uint16_t i = 0U; i < BSP_ADC_SCOPE_POINTS; i++)
    {
        adc_scope_points[i].x = (lv_coord_t)(10U +
            ((uint32_t)i * (ADC_SCOPE_WIDTH - 20U)) /
            (BSP_ADC_SCOPE_POINTS - 1U));

        if (span < 40U)
        {
            adc_scope_points[i].y = ADC_SCOPE_HEIGHT / 2;
        }
        else
        {
            uint32_t level = (uint32_t)(ui_adc_scope_frame.samples[i] -
                                         display_minimum);
            adc_scope_points[i].y = (lv_coord_t)(14U + (ADC_SCOPE_HEIGHT - 28U) -
                (level * (ADC_SCOPE_HEIGHT - 28U)) / span);
        }
    }

    /* Only this line object's old/new bounding area is invalidated.  The
       static background and other pages are not redrawn. */
    lv_line_set_points(adc_scope_line, adc_scope_points, BSP_ADC_SCOPE_POINTS);
}

/* Focused items have a thick white outline.  The actively edited item uses
   a static blue background and reversed white text, avoiding extra redraws. */
static void ui_update_edit_appearance(void)
{
    const lv_color_t normal_bg = lv_color_hex(0x172033);
    const lv_color_t normal_text = lv_color_hex(0xF0F4FA);
    const lv_color_t normal_border = lv_color_hex(0x426080);
    const lv_color_t blue = lv_color_hex(0x1677FF);

    for (uint8_t i = 0U; i < 6U; i++)
    {
        lv_obj_set_style_bg_color(settings_fields[i], normal_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
        /* An edit flash also sets the focused style.  Restore it here as
           well; otherwise its last blue/white background remains after
           leaving edit mode. */
        lv_obj_set_style_bg_color(settings_fields[i], normal_bg, LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(settings_fields[i], normal_border, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(settings_fields[i], 2, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(settings_fields[i], lv_color_white(), LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(settings_fields[i], 5, LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(settings_values[i], normal_text, LV_PART_MAIN | LV_STATE_DEFAULT);
    }

    if (ui_editing != 0U)
    {
        lv_color_t background = blue;
        lv_color_t foreground = lv_color_white();

        lv_obj_set_style_bg_color(settings_fields[ui_field], background, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(settings_fields[ui_field], background, LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(settings_fields[ui_field], foreground, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(settings_fields[ui_field], foreground, LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(settings_fields[ui_field], 7, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(settings_fields[ui_field], 7, LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(settings_values[ui_field], foreground, LV_PART_MAIN | LV_STATE_DEFAULT);
    }
}

static void ui_set_clock_hand(lv_obj_t *hand, lv_point_t points[2],
                              uint8_t tick, int16_t length)
{
    uint8_t cos_tick = (uint8_t)((tick + 15U) % 60U);

    points[0].x = CLOCK_DIAL_CENTER;
    points[0].y = CLOCK_DIAL_CENTER;
    points[1].x = (lv_coord_t)(CLOCK_DIAL_CENTER + ((clock_sin_60[tick] * length) / 1000));
    points[1].y = (lv_coord_t)(CLOCK_DIAL_CENTER - ((clock_sin_60[cos_tick] * length) / 1000));
    lv_line_set_points(hand, points, 2U);
}

static void ui_update_main_clock(void)
{
    RTC_TimeTypeDef time = {0};
    RTC_DateTypeDef date = {0};
    uint32_t stamp;

    (void)HAL_RTC_GetTime(&hrtc, &time, RTC_FORMAT_BIN);
    (void)HAL_RTC_GetDate(&hrtc, &date, RTC_FORMAT_BIN);

    stamp = (((((uint32_t)date.Year * 13U + date.Month) * 32U + date.Date) * 24U +
              time.Hours) * 60U + time.Minutes) * 60U + time.Seconds;
    if (stamp == ui_last_clock_stamp) return;
    ui_last_clock_stamp = stamp;

    lv_label_set_text_fmt(main_clock_label,
                          "20%02u-%02u-%02u\n%02u:%02u:%02u",
                          date.Year, date.Month, date.Date,
                          time.Hours, time.Minutes, time.Seconds);

    /* Updating only the line points invalidates the old/new hand areas;
       LVGL therefore performs a local refresh instead of a full-screen one. */
    ui_set_clock_hand(clock_hour_hand, clock_hour_points,
                      (uint8_t)(((time.Hours % 12U) * 5U + time.Minutes / 12U) % 60U), 74);
    ui_set_clock_hand(clock_minute_hand, clock_minute_points, time.Minutes, 112);
    ui_set_clock_hand(clock_second_hand, clock_second_points, time.Seconds, 130);
}

static void ui_adjust_value(int8_t direction)
{
    if (ui_field == 0U)
    {
        ui_time.Hours = (direction > 0) ?
                        (uint8_t)((ui_time.Hours + 1U) % 24U) :
                        (uint8_t)((ui_time.Hours == 0U) ? 23U : ui_time.Hours - 1U);
    }
    else if (ui_field == 1U)
    {
        ui_time.Minutes = (direction > 0) ?
                          (uint8_t)((ui_time.Minutes + 1U) % 60U) :
                          (uint8_t)((ui_time.Minutes == 0U) ? 59U : ui_time.Minutes - 1U);
    }
    else if (ui_field == 2U)
    {
        ui_time.Seconds = (direction > 0) ?
                          (uint8_t)((ui_time.Seconds + 1U) % 60U) :
                          (uint8_t)((ui_time.Seconds == 0U) ? 59U : ui_time.Seconds - 1U);
    }
    else if (ui_field == 3U)
    {
        ui_date.Year = (direction > 0) ?
                       (uint8_t)((ui_date.Year + 1U) % 100U) :
                       (uint8_t)((ui_date.Year == 0U) ? 99U : ui_date.Year - 1U);
    }
    else if (ui_field == 4U)
    {
        ui_date.Month = (direction > 0) ?
                        (uint8_t)((ui_date.Month >= 12U) ? 1U : ui_date.Month + 1U) :
                        (uint8_t)((ui_date.Month <= 1U) ? 12U : ui_date.Month - 1U);
        if (ui_date.Date > ui_days_in_month(ui_date.Year, ui_date.Month))
            ui_date.Date = ui_days_in_month(ui_date.Year, ui_date.Month);
    }
    else
    {
        uint8_t max_date = ui_days_in_month(ui_date.Year, ui_date.Month);
        ui_date.Date = (direction > 0) ?
                       (uint8_t)((ui_date.Date >= max_date) ? 1U : ui_date.Date + 1U) :
                       (uint8_t)((ui_date.Date <= 1U) ? max_date : ui_date.Date - 1U);
    }

    ui_update_settings_text();
}

static void ui_show_main(void);
static void ui_show_adc(void);
static void ui_show_settings(void);
static void ui_show_dac(void);
static void ui_show_alarm_menu(void);
static void ui_show_alarm_edit(uint8_t alarm_id);
static void ui_page_cache_use(uint8_t cache_slot);

static void ui_update_round_toggle(lv_obj_t *indicator, uint8_t on)
{
    const lv_color_t page_bg = lv_color_hex(0x0E1626);

    if (on != 0U)
    {
        /* Enabled: the small inner disc returns to the normal page colour,
           leaving the blue ring as the visible enabled mark. */
        lv_obj_set_style_bg_color(indicator, page_bg,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(indicator, lv_color_hex(0x1677FF),
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(indicator, 5,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
    }
    else
    {
        lv_obj_set_style_bg_color(indicator, lv_color_white(),
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(indicator, lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(indicator, 2,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
    }
}

static void ui_update_alarm_menu_text(void)
{
    BspAlarmConfig alarm_a;
    BspAlarmConfig alarm_b;

    BSP_Alarm_Get(BSP_ALARM_A, &alarm_a);
    BSP_Alarm_Get(BSP_ALARM_B, &alarm_b);
    lv_label_set_text_fmt(alarm_menu_values[0], "Time Chime   %s",
                          (ui_time_chime_enabled != 0U) ? "On" : "Off");
    lv_label_set_text_fmt(alarm_menu_values[1], "Alarm A   %02u:%02u:%02u   %s",
                          alarm_a.hours, alarm_a.minutes, alarm_a.seconds,
                          (alarm_a.enabled != 0U) ? "On" : "Off");
    lv_label_set_text_fmt(alarm_menu_values[2], "Alarm B   %02u:%02u:%02u   %s",
                          alarm_b.hours, alarm_b.minutes, alarm_b.seconds,
                          (alarm_b.enabled != 0U) ? "On" : "Off");
    ui_update_round_toggle(alarm_chime_indicator, ui_time_chime_enabled);
}

static void ui_update_alarm_edit_text(void)
{
    lv_label_set_text_fmt(alarm_edit_values[0], "Hour       %02u", ui_alarm_config.hours);
    lv_label_set_text_fmt(alarm_edit_values[1], "Minute     %02u", ui_alarm_config.minutes);
    lv_label_set_text_fmt(alarm_edit_values[2], "Second     %02u", ui_alarm_config.seconds);
    lv_label_set_text_fmt(alarm_edit_values[3], "Enabled    %s",
                          (ui_alarm_config.enabled != 0U) ? "On" : "Off");
    ui_update_round_toggle(alarm_edit_indicator, ui_alarm_config.enabled);
}

static void ui_update_alarm_edit_appearance(void)
{
    const lv_color_t normal_bg = lv_color_hex(0x172033);
    const lv_color_t normal_text = lv_color_hex(0xF0F4FA);
    const lv_color_t normal_border = lv_color_hex(0x426080);
    const lv_color_t blue = lv_color_hex(0x1677FF);

    for (uint8_t i = 0U; i < 4U; i++)
    {
        lv_obj_set_style_bg_color(alarm_edit_fields[i], normal_bg,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(alarm_edit_fields[i], normal_bg,
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(alarm_edit_fields[i], normal_border,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(alarm_edit_fields[i], 2,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(alarm_edit_fields[i], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(alarm_edit_fields[i], 5,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(alarm_edit_values[i], normal_text,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
    }

    if (ui_alarm_editing != 0U)
    {
        lv_obj_set_style_bg_color(alarm_edit_fields[ui_alarm_field], blue,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(alarm_edit_fields[ui_alarm_field], blue,
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(alarm_edit_fields[ui_alarm_field], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(alarm_edit_fields[ui_alarm_field], lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(alarm_edit_fields[ui_alarm_field], 7,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(alarm_edit_fields[ui_alarm_field], 7,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_text_color(alarm_edit_values[ui_alarm_field], lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
    }
}

static void ui_adjust_alarm_value(int8_t direction)
{
    if (ui_alarm_field == 0U)
    {
        ui_alarm_config.hours = (direction > 0) ?
            (uint8_t)((ui_alarm_config.hours + 1U) % 24U) :
            (uint8_t)((ui_alarm_config.hours == 0U) ? 23U : ui_alarm_config.hours - 1U);
    }
    else if (ui_alarm_field == 1U)
    {
        ui_alarm_config.minutes = (direction > 0) ?
            (uint8_t)((ui_alarm_config.minutes + 1U) % 60U) :
            (uint8_t)((ui_alarm_config.minutes == 0U) ? 59U : ui_alarm_config.minutes - 1U);
    }
    else if (ui_alarm_field == 2U)
    {
        ui_alarm_config.seconds = (direction > 0) ?
            (uint8_t)((ui_alarm_config.seconds + 1U) % 60U) :
            (uint8_t)((ui_alarm_config.seconds == 0U) ? 59U : ui_alarm_config.seconds - 1U);
    }
    else
    {
        ui_alarm_config.enabled = (uint8_t)!ui_alarm_config.enabled;
    }

    ui_update_alarm_edit_text();
}

static void ui_show_main(void)
{
    ui_page = UI_PAGE_MAIN;
    ui_editing = 0U;
    ui_dac_editing = 0U;
    ui_dac_selecting_frequency_digit = 0U;
    lv_scr_load(main_screen);
    lv_group_remove_all_objs(ui_group);
    for (uint8_t i = 0U; i < 5U; i++)
        lv_group_add_obj(ui_group, main_menu_buttons[i]);
    lv_group_focus_obj(main_menu_buttons[0]);
    ui_update_main_clock();
}

static void ui_show_adc(void)
{
    ui_page_cache_use(UI_CACHE_ADC);
    ui_page = UI_PAGE_ADC;
    ui_editing = 0U;
    ui_dac_editing = 0U;
    ui_dac_selecting_frequency_digit = 0U;
    lv_scr_load(adc_screen);
    lv_group_remove_all_objs(ui_group);
    lv_group_add_obj(ui_group, adc_back_button);
    lv_group_focus_obj(adc_back_button);
    ui_update_adc_scope();
}

static void ui_show_settings(void)
{
    ui_page_cache_use(UI_CACHE_SETTINGS);
    ui_page = UI_PAGE_SETTINGS;
    ui_editing = 0U;
    ui_dac_editing = 0U;
    ui_dac_selecting_frequency_digit = 0U;
    ui_read_rtc();
    lv_scr_load(settings_screen);
    lv_group_remove_all_objs(ui_group);

    for (uint8_t i = 0U; i < 6U; i++)
        lv_group_add_obj(ui_group, settings_fields[i]);

    lv_group_focus_obj(settings_fields[0]);
    ui_update_settings_text();
    ui_update_edit_appearance();
}

static void ui_show_dac(void)
{
    ui_page_cache_use(UI_CACHE_DAC);
    ui_page = UI_PAGE_DAC;
    ui_editing = 0U;
    ui_dac_editing = 0U;
    ui_dac_selecting_frequency_digit = 0U;
    lv_scr_load(dac_screen);
    lv_group_remove_all_objs(ui_group);

    for (uint8_t i = 0U; i < 5U; i++)
        lv_group_add_obj(ui_group, dac_fields[i]);

    lv_group_focus_obj(dac_fields[0]);
    ui_update_dac_text();
    ui_update_dac_appearance();
}

static void ui_show_alarm_menu(void)
{
    ui_page_cache_use(UI_CACHE_ALARM_MENU);
    ui_page = UI_PAGE_ALARM_MENU;
    ui_editing = 0U;
    ui_dac_editing = 0U;
    ui_dac_selecting_frequency_digit = 0U;
    ui_alarm_editing = 0U;
    lv_scr_load(alarm_screen);
    lv_group_remove_all_objs(ui_group);
    for (uint8_t i = 0U; i < 3U; i++)
        lv_group_add_obj(ui_group, alarm_menu_buttons[i]);
    lv_group_focus_obj(alarm_menu_buttons[0]);
    ui_update_alarm_menu_text();
}

static void ui_show_alarm_edit(uint8_t alarm_id)
{
    ui_page_cache_use(UI_CACHE_ALARM_EDIT);
    ui_alarm_id = alarm_id;
    BSP_Alarm_Get(alarm_id, &ui_alarm_config);
    ui_page = UI_PAGE_ALARM_EDIT;
    ui_alarm_editing = 0U;
    lv_label_set_text_fmt(alarm_edit_title, "Alarm %c Settings",
                          (alarm_id == BSP_ALARM_A) ? 'A' : 'B');
    lv_scr_load(alarm_edit_screen);
    lv_group_remove_all_objs(ui_group);
    for (uint8_t i = 0U; i < 4U; i++)
        lv_group_add_obj(ui_group, alarm_edit_fields[i]);
    lv_group_focus_obj(alarm_edit_fields[0]);
    ui_update_alarm_edit_text();
    ui_update_alarm_edit_appearance();
}

static void ui_restore_after_alarm(void)
{
    switch (ui_page_before_alarm)
    {
        case UI_PAGE_SETTINGS:   ui_show_settings(); break;
        case UI_PAGE_DAC:        ui_show_dac(); break;
        case UI_PAGE_ADC:        ui_show_adc(); break;
        case UI_PAGE_ALARM_MENU: ui_show_alarm_menu(); break;
        case UI_PAGE_ALARM_EDIT: ui_show_alarm_edit(ui_alarm_id); break;
        case UI_PAGE_MAIN:
        default:                 ui_show_main(); break;
    }
}

static void ui_alarm_finish(uint8_t action)
{
    BSP_Buzzer_Set(0U);
    ui_alarm_ringing = 0U;

    if (action == 0U) BSP_Alarm_Dismiss(ui_active_alarm_id);
    else if (action == 1U) BSP_Alarm_Snooze(ui_active_alarm_id);
    else BSP_Alarm_Timeout(ui_active_alarm_id);

    ui_restore_after_alarm();
}

static void ui_alarm_start(uint8_t alarm_id, uint32_t now)
{
    ui_active_alarm_id = alarm_id;
    ui_page_before_alarm = ui_page;
    ui_alarm_ringing = 1U;
    ui_alarm_started_tick = now;
    ui_buzzer_phase = 0U;
    ui_buzzer_deadline = now + 120U;
    BSP_Buzzer_Set(1U);

    lv_label_set_text_fmt(alarm_popup_title, "Alarm %c", (alarm_id == BSP_ALARM_A) ? 'A' : 'B');
    lv_label_set_text(alarm_popup_hint,
                      "KEY1: Stop\nWK_UP: Snooze 5 minutes\nNo key for 30 s: automatic snooze");
    ui_page = UI_PAGE_ALARM_POPUP;
    lv_scr_load(alarm_popup_screen);
    lv_group_remove_all_objs(ui_group);
    lv_group_add_obj(ui_group, alarm_popup_close_button);
    lv_group_focus_obj(alarm_popup_close_button);
}

static void ui_alarm_buzzer_update(uint32_t now)
{
    static const uint16_t phase_ms[6] = {120U, 120U, 120U, 120U, 120U, 1000U};

    if (ui_alarm_ringing == 0U) return;
    if ((int32_t)(now - ui_buzzer_deadline) < 0) return;

    ui_buzzer_phase = (uint8_t)((ui_buzzer_phase + 1U) % 6U);
    BSP_Buzzer_Set((ui_buzzer_phase & 1U) == 0U ? 1U : 0U);
    ui_buzzer_deadline = now + phase_ms[ui_buzzer_phase];
}

static uint8_t ui_is_editing(void)
{
    if (ui_page == UI_PAGE_DAC)
        return (uint8_t)((ui_dac_editing != 0U) ||
                         (ui_dac_selecting_frequency_digit != 0U));
    if (ui_page == UI_PAGE_ALARM_EDIT) return ui_alarm_editing;
    return ui_editing;
}

static void ui_button_event(lv_event_t *event)
{
    lv_event_code_t code = lv_event_get_code(event);
    lv_obj_t *target = lv_event_get_target(event);

    if (code == LV_EVENT_CLICKED)
    {
        for (uint8_t i = 0U; i < 5U; i++)
        {
            if (target == main_menu_buttons[i])
            {
                if (i == 0U) ui_show_settings();
                else if (i == 1U) ui_show_adc();
                else if (i == 2U) ui_show_dac();
                else if (i == 3U) ui_show_alarm_menu();
                /* Game is intentionally still a placeholder. */
                return;
            }
        }

        if (target == adc_back_button)
        {
            ui_show_main();
            return;
        }

        if (target == alarm_popup_close_button)
        {
            /* KEY1 always stops the currently ringing alarm. */
            ui_alarm_finish(0U);
            return;
        }

        for (uint8_t i = 0U; i < 3U; i++)
        {
            if (target == alarm_menu_buttons[i])
            {
                if (i == 0U)
                {
                    /* UI-only for now: no timed sound behaviour is attached
                       until the required broadcast content is defined. */
                    ui_time_chime_enabled = (uint8_t)!ui_time_chime_enabled;
                    ui_update_alarm_menu_text();
                }
                else
                {
                    ui_show_alarm_edit((i == 1U) ? BSP_ALARM_A : BSP_ALARM_B);
                }
                return;
            }
        }

        for (uint8_t i = 0U; i < 4U; i++)
        {
            if (target == alarm_edit_fields[i])
            {
                ui_alarm_field = i;
                if (i == 3U)
                {
                    ui_alarm_config.enabled = (uint8_t)!ui_alarm_config.enabled;
                    (void)BSP_Alarm_Set(ui_alarm_id, &ui_alarm_config);
                    ui_update_alarm_edit_text();
                    ui_update_alarm_edit_appearance();
                }
                else if (ui_alarm_editing == 0U)
                {
                    /* A second confirm deliberately does nothing while a
                       numeric field is active; WK_UP commits and exits it. */
                    ui_alarm_editing = 1U;
                    ui_update_alarm_edit_appearance();
                }
                return;
            }
        }

        for (uint8_t i = 0U; i < 6U; i++)
        {
            if (target == settings_fields[i])
            {
                ui_field = i;
                /* Confirm enters edit mode once.  A second confirm while
                   already editing deliberately has no effect; WK_UP is the
                   only key used to leave the current level. */
                if (ui_editing == 0U)
                {
                    ui_editing = 1U;
                    ui_update_settings_text();
                    ui_update_edit_appearance();
                }
                return;
            }
        }

        for (uint8_t i = 0U; i < 5U; i++)
        {
            if (target == dac_fields[i])
            {
                ui_dac_field = i;
                if (i == 4U)
                {
                    /* Output is an action button, not an editable value. */
                    ui_dac_config.running = (uint8_t)!ui_dac_config.running;
                    ui_apply_dac_config();
                    ui_update_dac_appearance();
                }
                else if (i == 3U)
                {
                    if ((ui_dac_editing == 0U) &&
                        (ui_dac_selecting_frequency_digit == 0U))
                    {
                        /* First confirm selects a position.  Start on the
                           ones digit so one press changes 1 Hz by default. */
                        ui_dac_frequency_digit = UI_DAC_FREQ_DIGIT_ONES;
                        ui_dac_selecting_frequency_digit = 1U;
                    }
                    else if (ui_dac_selecting_frequency_digit != 0U)
                    {
                        /* Confirm the selected position, then edit it. */
                        ui_dac_selecting_frequency_digit = 0U;
                        ui_dac_editing = 1U;
                    }
                    ui_update_dac_text();
                    ui_update_dac_appearance();
                }
                else if ((ui_dac_editing == 0U) &&
                         (ui_dac_selecting_frequency_digit == 0U))
                {
                    ui_dac_editing = 1U;
                    ui_update_dac_appearance();
                }
                return;
            }
        }
    }

    if (code == LV_EVENT_KEY)
    {
        uint32_t key = lv_event_get_key(event);

        if (key == LV_KEY_ESC)
        {
            if (ui_page == UI_PAGE_ALARM_POPUP)
            {
                /* WK_UP always means snooze on a ringing-alarm page. */
                ui_alarm_finish(1U);
            }
            else if (ui_page == UI_PAGE_ALARM_EDIT)
            {
                if (ui_alarm_editing != 0U)
                {
                    (void)BSP_Alarm_Set(ui_alarm_id, &ui_alarm_config);
                    ui_alarm_editing = 0U;
                    ui_update_alarm_edit_text();
                    ui_update_alarm_edit_appearance();
                }
                else
                {
                    ui_show_alarm_menu();
                }
            }
            else if (ui_page == UI_PAGE_ALARM_MENU)
            {
                ui_show_main();
            }
            else if (ui_page == UI_PAGE_DAC)
            {
                if (ui_dac_editing != 0U)
                {
                    ui_dac_editing = 0U;
                    if (ui_dac_field == 3U)
                    {
                        /* Numeric frequency edit -> digit selection. */
                        ui_dac_selecting_frequency_digit = 1U;
                    }
                    else
                    {
                        ui_apply_dac_config();
                    }
                    ui_update_dac_text();
                    ui_update_dac_appearance();
                }
                else if (ui_dac_selecting_frequency_digit != 0U)
                {
                    /* Digit selection -> ordinary DAC field list. */
                    ui_dac_selecting_frequency_digit = 0U;
                    ui_update_dac_text();
                    ui_update_dac_appearance();
                }
                else
                {
                    ui_show_main();
                }
            }
            else if (ui_editing != 0U)
            {
                /* Editing -> settings list is one level up.  Commit the
                   displayed value here, rather than on a second confirm. */
                ui_write_rtc();
                ui_editing = 0U;
                ui_update_settings_text();
                ui_update_edit_appearance();
            }
            else
            {
                ui_show_main();
            }
        }
        else if (ui_page == UI_PAGE_DAC &&
                 ui_dac_selecting_frequency_digit != 0U &&
                 key == LV_KEY_LEFT)
        {
            /* Only digit selection is reversed: KEY2 moves toward the
               higher place value, while numeric decrement stays unchanged. */
            ui_select_dac_frequency_digit(1);
            ui_update_dac_appearance();
        }
        else if (ui_page == UI_PAGE_DAC &&
                 ui_dac_selecting_frequency_digit != 0U &&
                 key == LV_KEY_RIGHT)
        {
            /* KEY0 moves toward the lower place value during selection.
               In numeric edit it still increases the selected value. */
            ui_select_dac_frequency_digit(-1);
            ui_update_dac_appearance();
        }
        else if (ui_page == UI_PAGE_DAC && ui_dac_editing != 0U &&
                 key == LV_KEY_LEFT)
        {
            ui_adjust_dac_value(-1);
            ui_update_dac_appearance();
        }
        else if (ui_page == UI_PAGE_DAC && ui_dac_editing != 0U &&
                 key == LV_KEY_RIGHT)
        {
            ui_adjust_dac_value(1);
            ui_update_dac_appearance();
        }
        else if (ui_page == UI_PAGE_SETTINGS && ui_editing != 0U &&
                 key == LV_KEY_LEFT)
        {
            ui_adjust_value(-1);
            ui_update_edit_appearance();
        }
        else if (ui_page == UI_PAGE_SETTINGS && ui_editing != 0U &&
                 key == LV_KEY_RIGHT)
        {
            ui_adjust_value(1);
            ui_update_edit_appearance();
        }
        else if (ui_page == UI_PAGE_ALARM_EDIT && ui_alarm_editing != 0U &&
                 key == LV_KEY_LEFT)
        {
            ui_adjust_alarm_value(-1);
            ui_update_alarm_edit_appearance();
        }
        else if (ui_page == UI_PAGE_ALARM_EDIT && ui_alarm_editing != 0U &&
                 key == LV_KEY_RIGHT)
        {
            ui_adjust_alarm_value(1);
            ui_update_alarm_edit_appearance();
        }
    }
}

static uint32_t ui_map_key(uint8_t key)
{
    switch (key)
    {
        /* Board key order returned by BSP_Key_Read():
           KEY0=1, KEY1=2, KEY2=3, WK_UP=4.
           KEY1 confirms; WK_UP returns; KEY0/KEY2 navigate or +/-.
           KEY0 is deliberately the positive/next direction. */
        case 1U: return (ui_is_editing() != 0U) ? LV_KEY_RIGHT : LV_KEY_NEXT;
        case 2U: return LV_KEY_ENTER;
        case 3U: return (ui_is_editing() != 0U) ? LV_KEY_LEFT : LV_KEY_PREV;
        case 4U: return LV_KEY_ESC;
        default: return 0U;
    }
}

static void lvgl_keypad_read(lv_indev_drv_t *indev_drv,
                             lv_indev_data_t *data)
{
    uint8_t key = BSP_Key_Read();
    (void)indev_drv;

    data->state = (key == 0U) ? LV_INDEV_STATE_REL : LV_INDEV_STATE_PR;
    data->key = ui_map_key(key);
}

/* Draw all 60 tick marks through one transparent LVGL object.  Making one
   lv_line object per tick consumed too much of the fixed 40 KiB LVGL pool
   and could stop the UI during lv_init/ui_create. */
static void ui_clock_ticks_draw_event(lv_event_t *event)
{
    lv_obj_t *object;
    lv_draw_ctx_t *draw_ctx;
    lv_area_t area;
    lv_draw_line_dsc_t line_dsc;

    if (lv_event_get_code(event) != LV_EVENT_DRAW_MAIN) return;
    object = lv_event_get_target(event);
    draw_ctx = lv_event_get_draw_ctx(event);
    lv_obj_get_coords(object, &area);
    lv_draw_line_dsc_init(&line_dsc);

    for (uint8_t tick = 0U; tick < 60U; tick++)
    {
        uint8_t cos_tick = (uint8_t)((tick + 15U) % 60U);
        uint8_t is_hour = (uint8_t)((tick % 5U) == 0U);
        int16_t outer_radius = 148;
        int16_t inner_radius = (is_hour != 0U) ? 132 : 140;
        lv_point_t p1;
        lv_point_t p2;

        p1.x = (lv_coord_t)(area.x1 + CLOCK_DIAL_CENTER +
            (clock_sin_60[tick] * outer_radius) / 1000);
        p1.y = (lv_coord_t)(area.y1 + CLOCK_DIAL_CENTER -
            (clock_sin_60[cos_tick] * outer_radius) / 1000);
        p2.x = (lv_coord_t)(area.x1 + CLOCK_DIAL_CENTER +
            (clock_sin_60[tick] * inner_radius) / 1000);
        p2.y = (lv_coord_t)(area.y1 + CLOCK_DIAL_CENTER -
            (clock_sin_60[cos_tick] * inner_radius) / 1000);
        line_dsc.color = (is_hour != 0U) ? lv_color_white() : lv_color_hex(0x6D809B);
        line_dsc.width = (is_hour != 0U) ? 3 : 1;
        line_dsc.round_start = 0U;
        line_dsc.round_end = 0U;
        lv_draw_line(draw_ctx, &line_dsc, &p1, &p2);
    }
}

static lv_obj_t *ui_create_page_screen(void)
{
    lv_obj_t *screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x0E1626),
                              LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER,
                            LV_PART_MAIN | LV_STATE_DEFAULT);
    return screen;
}

static void ui_style_button(lv_obj_t *button)
{
    lv_obj_set_style_radius(button, 10, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x172033),
                              LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x172033),
                              LV_PART_MAIN | LV_STATE_FOCUSED);
    lv_obj_set_style_border_color(button, lv_color_hex(0x426080),
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(button, 2, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(button, lv_color_white(),
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(button, 5, LV_PART_MAIN | LV_STATE_FOCUSED);
}

static void ui_create_settings_page(void)
{
    static const char *field_names[6] =
        {"Hour", "Minute", "Second", "Year", "Month", "Date"};
    lv_obj_t *title;

    settings_screen = ui_create_page_screen();
    title = lv_label_create(settings_screen);
    lv_label_set_text(title, "Adjust Time / Date");
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

    for (uint8_t i = 0U; i < 6U; i++)
    {
        settings_fields[i] = lv_btn_create(settings_screen);
        lv_obj_set_size(settings_fields[i], 430, 88);
        lv_obj_align(settings_fields[i], LV_ALIGN_TOP_MID, 0, 78 + (i * 112));
        ui_style_button(settings_fields[i]);
        lv_obj_set_style_text_font(settings_fields[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_add_event_cb(settings_fields[i], ui_button_event, LV_EVENT_ALL, NULL);
        settings_values[i] = lv_label_create(settings_fields[i]);
        lv_obj_set_style_text_font(settings_values[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_label_set_text(settings_values[i], field_names[i]);
        lv_obj_center(settings_values[i]);
    }
}

static void ui_create_dac_page(void)
{
    static const char *dac_field_names[5] =
        {"Waveform", "Vpp", "Duty", "Frequency", "Output"};
    lv_obj_t *title;
    lv_obj_t *frequency_row;

    dac_screen = ui_create_page_screen();
    title = lv_label_create(dac_screen);
    lv_label_set_text(title, "DAC Waveform Output");
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

    for (uint8_t i = 0U; i < 5U; i++)
    {
        dac_fields[i] = lv_btn_create(dac_screen);
        lv_obj_set_size(dac_fields[i], 430, 88);
        lv_obj_align(dac_fields[i], LV_ALIGN_TOP_MID, 0, 78 + (i * 112));
        ui_style_button(dac_fields[i]);
        lv_obj_set_style_text_font(dac_fields[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_add_event_cb(dac_fields[i], ui_button_event, LV_EVENT_ALL, NULL);
        dac_values[i] = lv_label_create(dac_fields[i]);
        lv_obj_set_style_text_font(dac_values[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_label_set_text(dac_values[i], dac_field_names[i]);
        lv_obj_center(dac_values[i]);
    }

    lv_obj_del(dac_values[3]);
    frequency_row = lv_obj_create(dac_fields[3]);
    lv_obj_set_size(frequency_row, 300, 38);
    lv_obj_align(frequency_row, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_opa(frequency_row, LV_OPA_TRANSP, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(frequency_row, 0, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_pad_all(frequency_row, 0, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_clear_flag(frequency_row, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(frequency_row, LV_OBJ_FLAG_CLICKABLE);

    dac_values[3] = lv_label_create(frequency_row);
    lv_label_set_text(dac_values[3], "Frequency");
    lv_obj_set_style_text_font(dac_values[3], &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(dac_values[3], LV_ALIGN_LEFT_MID, 10, 0);
    for (uint8_t i = 0U; i < 5U; i++)
    {
        static const lv_coord_t digit_x[5] = {150, 169, 188, 207, 235};
        dac_frequency_digits[i] = lv_label_create(frequency_row);
        lv_obj_set_width(dac_frequency_digits[i], 18);
        lv_obj_align(dac_frequency_digits[i], LV_ALIGN_LEFT_MID, digit_x[i], 0);
        lv_obj_set_style_text_align(dac_frequency_digits[i], LV_TEXT_ALIGN_CENTER,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(dac_frequency_digits[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_color(dac_frequency_digits[i], lv_color_hex(0xF0F4FA),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_label_set_text(dac_frequency_digits[i], "0");
    }
    dac_frequency_dot = lv_label_create(frequency_row);
    lv_label_set_text(dac_frequency_dot, ".");
    lv_obj_align(dac_frequency_dot, LV_ALIGN_LEFT_MID, 226, 0);
    lv_obj_set_style_text_font(dac_frequency_dot, &lv_font_montserrat_24,
                               LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_color(dac_frequency_dot, lv_color_hex(0xF0F4FA),
                                LV_PART_MAIN | LV_STATE_DEFAULT);
    dac_frequency_unit = lv_label_create(frequency_row);
    lv_label_set_text(dac_frequency_unit, "Hz");
    lv_obj_align(dac_frequency_unit, LV_ALIGN_LEFT_MID, 257, 0);
    lv_obj_set_style_text_font(dac_frequency_unit, &lv_font_montserrat_24,
                               LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_color(dac_frequency_unit, lv_color_hex(0xF0F4FA),
                                LV_PART_MAIN | LV_STATE_DEFAULT);
    dac_frequency_box = lv_obj_create(frequency_row);
    lv_obj_set_size(dac_frequency_box, 20, 32);
    lv_obj_set_style_bg_opa(dac_frequency_box, LV_OPA_TRANSP, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(dac_frequency_box, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(dac_frequency_box, 2, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_radius(dac_frequency_box, 3, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_clear_flag(dac_frequency_box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(dac_frequency_box, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(dac_frequency_box, LV_OBJ_FLAG_HIDDEN);
}

static void ui_create_adc_page(void)
{
    lv_obj_t *title;
    lv_obj_t *back_label;

    adc_screen = ui_create_page_screen();
    title = lv_label_create(adc_screen);
    lv_label_set_text(title, "ADC Scope");
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);
    adc_frequency_label = lv_label_create(adc_screen);
    lv_label_set_text(adc_frequency_label, "Freq: --");
    lv_obj_set_style_text_color(adc_frequency_label, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(adc_frequency_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(adc_frequency_label, LV_ALIGN_TOP_MID, 0, 50);
    adc_vpp_label = lv_label_create(adc_screen);
    lv_label_set_text(adc_vpp_label, "Vpp: --");
    lv_obj_set_style_text_color(adc_vpp_label, lv_color_hex(0xA9D5FF), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(adc_vpp_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(adc_vpp_label, LV_ALIGN_TOP_MID, 0, 82);
    adc_wave_label = lv_label_create(adc_screen);
    lv_label_set_text(adc_wave_label, "Wave: --");
    lv_obj_set_style_text_color(adc_wave_label, lv_color_hex(0xA9D5FF), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(adc_wave_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(adc_wave_label, LV_ALIGN_TOP_MID, 0, 114);
    adc_scope_panel = lv_obj_create(adc_screen);
    lv_obj_set_size(adc_scope_panel, 430, 250);
    lv_obj_align(adc_scope_panel, LV_ALIGN_TOP_MID, 0, 155);
    lv_obj_clear_flag(adc_scope_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(adc_scope_panel, 10, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(adc_scope_panel, lv_color_hex(0x172033), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(adc_scope_panel, lv_color_hex(0x426080), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(adc_scope_panel, 2, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_pad_all(adc_scope_panel, 0, LV_PART_MAIN | LV_STATE_DEFAULT);
    adc_scope_line = lv_line_create(adc_scope_panel);
    lv_obj_set_style_line_color(adc_scope_line, lv_color_hex(0x49D17D), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_line_width(adc_scope_line, 2, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_line_rounded(adc_scope_line, true, LV_PART_MAIN | LV_STATE_DEFAULT);
    adc_back_button = lv_btn_create(adc_screen);
    lv_obj_set_size(adc_back_button, 260, 64);
    lv_obj_align(adc_back_button, LV_ALIGN_TOP_MID, 0, 430);
    ui_style_button(adc_back_button);
    lv_obj_add_event_cb(adc_back_button, ui_button_event, LV_EVENT_ALL, NULL);
    back_label = lv_label_create(adc_back_button);
    lv_label_set_text(back_label, "UP: Back");
    lv_obj_set_style_text_color(back_label, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(back_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_center(back_label);
}

static void ui_create_alarm_menu_page(void)
{
    lv_obj_t *title;

    alarm_screen = ui_create_page_screen();
    title = lv_label_create(alarm_screen);
    lv_label_set_text(title, "Alarm");
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 20);
    for (uint8_t i = 0U; i < 3U; i++)
    {
        alarm_menu_buttons[i] = lv_btn_create(alarm_screen);
        lv_obj_set_size(alarm_menu_buttons[i], 430, 100);
        lv_obj_align(alarm_menu_buttons[i], LV_ALIGN_TOP_MID, 0, 85 + i * 125);
        ui_style_button(alarm_menu_buttons[i]);
        lv_obj_add_event_cb(alarm_menu_buttons[i], ui_button_event, LV_EVENT_ALL, NULL);
        alarm_menu_values[i] = lv_label_create(alarm_menu_buttons[i]);
        lv_obj_set_style_text_color(alarm_menu_values[i], lv_color_hex(0xF0F4FA),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(alarm_menu_values[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(alarm_menu_values[i], LV_ALIGN_LEFT_MID, 24, 0);
    }
    alarm_chime_indicator = lv_obj_create(alarm_menu_buttons[0]);
    lv_obj_set_size(alarm_chime_indicator, 34, 34);
    lv_obj_align(alarm_chime_indicator, LV_ALIGN_RIGHT_MID, -24, 0);
    lv_obj_clear_flag(alarm_chime_indicator, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(alarm_chime_indicator, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_radius(alarm_chime_indicator, LV_RADIUS_CIRCLE, LV_PART_MAIN | LV_STATE_DEFAULT);
}

static void ui_create_alarm_edit_page(void)
{
    alarm_edit_screen = ui_create_page_screen();
    alarm_edit_title = lv_label_create(alarm_edit_screen);
    lv_obj_set_style_text_color(alarm_edit_title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(alarm_edit_title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(alarm_edit_title, LV_ALIGN_TOP_MID, 0, 20);
    for (uint8_t i = 0U; i < 4U; i++)
    {
        alarm_edit_fields[i] = lv_btn_create(alarm_edit_screen);
        lv_obj_set_size(alarm_edit_fields[i], 430, 105);
        lv_obj_align(alarm_edit_fields[i], LV_ALIGN_TOP_MID, 0, 85 + i * 120);
        ui_style_button(alarm_edit_fields[i]);
        lv_obj_add_event_cb(alarm_edit_fields[i], ui_button_event, LV_EVENT_ALL, NULL);
        alarm_edit_values[i] = lv_label_create(alarm_edit_fields[i]);
        lv_obj_set_style_text_font(alarm_edit_values[i], &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(alarm_edit_values[i], LV_ALIGN_LEFT_MID, 24, 0);
    }
    alarm_edit_indicator = lv_obj_create(alarm_edit_fields[3]);
    lv_obj_set_size(alarm_edit_indicator, 34, 34);
    lv_obj_align(alarm_edit_indicator, LV_ALIGN_RIGHT_MID, -24, 0);
    lv_obj_clear_flag(alarm_edit_indicator, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(alarm_edit_indicator, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_radius(alarm_edit_indicator, LV_RADIUS_CIRCLE, LV_PART_MAIN | LV_STATE_DEFAULT);
}

static uint8_t ui_cache_slot_from_page(uint8_t page)
{
    switch (page)
    {
        case UI_PAGE_SETTINGS: return UI_CACHE_SETTINGS;
        case UI_PAGE_DAC: return UI_CACHE_DAC;
        case UI_PAGE_ADC: return UI_CACHE_ADC;
        case UI_PAGE_ALARM_MENU: return UI_CACHE_ALARM_MENU;
        case UI_PAGE_ALARM_EDIT: return UI_CACHE_ALARM_EDIT;
        default: return UI_CACHE_SLOT_COUNT;
    }
}

static void ui_page_cache_destroy(uint8_t cache_slot)
{
    if (cache_slot == UI_CACHE_SETTINGS)
    {
        if (settings_screen != NULL) lv_obj_del(settings_screen);
        settings_screen = NULL;
        for (uint8_t i = 0U; i < 6U; i++) { settings_fields[i] = NULL; settings_values[i] = NULL; }
    }
    else if (cache_slot == UI_CACHE_DAC)
    {
        if (dac_screen != NULL) lv_obj_del(dac_screen);
        dac_screen = NULL;
        for (uint8_t i = 0U; i < 5U; i++) { dac_fields[i] = NULL; dac_values[i] = NULL; dac_frequency_digits[i] = NULL; }
        dac_frequency_dot = NULL; dac_frequency_unit = NULL; dac_frequency_box = NULL;
    }
    else if (cache_slot == UI_CACHE_ADC)
    {
        if (adc_screen != NULL) lv_obj_del(adc_screen);
        adc_screen = NULL; adc_frequency_label = NULL; adc_vpp_label = NULL;
        adc_wave_label = NULL; adc_scope_panel = NULL; adc_scope_line = NULL; adc_back_button = NULL;
    }
    else if (cache_slot == UI_CACHE_ALARM_MENU)
    {
        if (alarm_screen != NULL) lv_obj_del(alarm_screen);
        alarm_screen = NULL; alarm_chime_indicator = NULL;
        for (uint8_t i = 0U; i < 3U; i++) { alarm_menu_buttons[i] = NULL; alarm_menu_values[i] = NULL; }
    }
    else if (cache_slot == UI_CACHE_ALARM_EDIT)
    {
        if (alarm_edit_screen != NULL) lv_obj_del(alarm_edit_screen);
        alarm_edit_screen = NULL; alarm_edit_title = NULL; alarm_edit_indicator = NULL;
        for (uint8_t i = 0U; i < 4U; i++) { alarm_edit_fields[i] = NULL; alarm_edit_values[i] = NULL; }
    }
    ui_page_cache_stamp[cache_slot] = 0U;
}

static void ui_page_cache_create(uint8_t cache_slot)
{
    if (cache_slot == UI_CACHE_SETTINGS) ui_create_settings_page();
    else if (cache_slot == UI_CACHE_DAC) ui_create_dac_page();
    else if (cache_slot == UI_CACHE_ADC) ui_create_adc_page();
    else if (cache_slot == UI_CACHE_ALARM_MENU) ui_create_alarm_menu_page();
    else if (cache_slot == UI_CACHE_ALARM_EDIT) ui_create_alarm_edit_page();
}

static void ui_page_cache_use(uint8_t cache_slot)
{
    uint8_t active_count = 0U;

    if (ui_page_cache_stamp[cache_slot] == 0U)
    {
        for (uint8_t i = 0U; i < UI_CACHE_SLOT_COUNT; i++)
            if (ui_page_cache_stamp[i] != 0U) active_count++;

        if (active_count >= UI_PAGE_CACHE_CAPACITY)
        {
            uint8_t current_slot = ui_cache_slot_from_page(ui_page);
            uint8_t oldest_slot = UI_CACHE_SLOT_COUNT;
            uint32_t oldest_stamp = 0xFFFFFFFFU;
            for (uint8_t i = 0U; i < UI_CACHE_SLOT_COUNT; i++)
            {
                if (i != current_slot && ui_page_cache_stamp[i] != 0U &&
                    ui_page_cache_stamp[i] < oldest_stamp)
                {
                    oldest_stamp = ui_page_cache_stamp[i];
                    oldest_slot = i;
                }
            }
            if (oldest_slot < UI_CACHE_SLOT_COUNT) ui_page_cache_destroy(oldest_slot);
        }
        ui_page_cache_create(cache_slot);
    }
    ui_page_cache_stamp[cache_slot] = ++ui_page_cache_clock;
}

static void ui_create(void)
{
    const lv_color_t page_bg = lv_color_hex(0x0E1626);
    const lv_color_t panel_bg = lv_color_hex(0x172033);
    const lv_color_t panel_border = lv_color_hex(0x426080);

    main_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(main_screen, page_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(main_screen, LV_OPA_COVER, LV_PART_MAIN | LV_STATE_DEFAULT);

#if (LVGL_DIAG_UI_STAGE == 1U)
    /* DIAG: Labels, buttons, input device and initial screen load are bypassed. */
    return;
#endif

    alarm_popup_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(alarm_popup_screen, page_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(alarm_popup_screen, LV_OPA_COVER, LV_PART_MAIN | LV_STATE_DEFAULT);

#if (LVGL_DIAG_UI_STAGE == 2U)
    /* DIAG: Widget creation is intentionally bypassed. */
    return;
#endif

    main_clock_label = lv_label_create(main_screen);
    lv_obj_set_style_text_color(main_clock_label, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(main_clock_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(main_clock_label, LV_ALIGN_TOP_MID, 0, 10);

    clock_face = lv_obj_create(main_screen);
    lv_obj_set_size(clock_face, CLOCK_DIAL_SIZE, CLOCK_DIAL_SIZE);
    lv_obj_align(clock_face, LV_ALIGN_TOP_MID, 0, 95);
    lv_obj_clear_flag(clock_face, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(clock_face, LV_RADIUS_CIRCLE, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(clock_face, panel_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(clock_face, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(clock_face, 4, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_pad_all(clock_face, 0, LV_PART_MAIN | LV_STATE_DEFAULT);

    /* 60 ticks make the dial readable without cluttering it with minute
       numbers.  A single custom draw layer avoids allocating 60 lv_line
       objects from the limited LVGL memory pool. */
    {
        lv_obj_t *clock_ticks_layer = lv_obj_create(clock_face);
        lv_obj_set_size(clock_ticks_layer, CLOCK_DIAL_SIZE, CLOCK_DIAL_SIZE);
        lv_obj_align(clock_ticks_layer, LV_ALIGN_CENTER, 0, 0);
        lv_obj_clear_flag(clock_ticks_layer, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(clock_ticks_layer, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_bg_opa(clock_ticks_layer, LV_OPA_TRANSP,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(clock_ticks_layer, 0,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_pad_all(clock_ticks_layer, 0,
                                 LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_add_event_cb(clock_ticks_layer, ui_clock_ticks_draw_event,
                            LV_EVENT_DRAW_MAIN, NULL);
    }

    /* Only 12 text objects remain: one label for each hour. */
    for (uint8_t tick = 0U; tick < 60U; tick++)
    {
        uint8_t cos_tick = (uint8_t)((tick + 15U) % 60U);
        uint8_t is_hour = (uint8_t)((tick % 5U) == 0U);

        if (is_hour != 0U)
        {
            lv_obj_t *hour_number = lv_label_create(clock_face);
            uint8_t hour = (tick == 0U) ? 12U : (uint8_t)(tick / 5U);
            int16_t number_radius = 112;

            lv_label_set_text_fmt(hour_number, "%u", hour);
            lv_obj_set_size(hour_number, 30, 22);
            lv_obj_set_style_text_align(hour_number, LV_TEXT_ALIGN_CENTER,
                                        LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_color(hour_number, lv_color_hex(0xD8E8FF),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_font(hour_number, &lv_font_montserrat_14,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_pos(hour_number,
                (lv_coord_t)(CLOCK_DIAL_CENTER +
                             (clock_sin_60[tick] * number_radius) / 1000 - 15),
                (lv_coord_t)(CLOCK_DIAL_CENTER -
                             (clock_sin_60[cos_tick] * number_radius) / 1000 - 11));
        }
    }

    clock_hour_hand = lv_line_create(clock_face);
    lv_obj_set_style_line_color(clock_hour_hand, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_line_width(clock_hour_hand, 8, LV_PART_MAIN | LV_STATE_DEFAULT);
    clock_minute_hand = lv_line_create(clock_face);
    lv_obj_set_style_line_color(clock_minute_hand, lv_color_hex(0xA9D5FF), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_line_width(clock_minute_hand, 5, LV_PART_MAIN | LV_STATE_DEFAULT);
    clock_second_hand = lv_line_create(clock_face);
    lv_obj_set_style_line_color(clock_second_hand, lv_color_hex(0x1677FF), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_line_width(clock_second_hand, 2, LV_PART_MAIN | LV_STATE_DEFAULT);

    lv_obj_t *clock_center = lv_obj_create(clock_face);
    lv_obj_set_size(clock_center, 18, 18);
    lv_obj_align(clock_center, LV_ALIGN_CENTER, 0, 0);
    lv_obj_clear_flag(clock_center, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(clock_center, LV_RADIUS_CIRCLE, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(clock_center, lv_color_hex(0x1677FF), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(clock_center, 0, LV_PART_MAIN | LV_STATE_DEFAULT);

    {
        static const char *main_menu_names[5] =
            {"Time Settings", "ADC", "DAC", "Alarm", "Game"};

        for (uint8_t i = 0U; i < 5U; i++)
        {
            main_menu_buttons[i] = lv_btn_create(main_screen);
            lv_obj_set_size(main_menu_buttons[i], 430, 60);
            lv_obj_align(main_menu_buttons[i], LV_ALIGN_TOP_MID, 0,
                         454 + (i * 65));
            lv_obj_set_style_radius(main_menu_buttons[i], 10,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_bg_color(main_menu_buttons[i], panel_bg,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_bg_color(main_menu_buttons[i], panel_bg,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_set_style_border_color(main_menu_buttons[i], panel_border,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_width(main_menu_buttons[i], 2,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_color(main_menu_buttons[i], lv_color_white(),
                                          LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_set_style_border_width(main_menu_buttons[i], 5,
                                          LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_add_event_cb(main_menu_buttons[i], ui_button_event,
                                LV_EVENT_ALL, NULL);

            lv_obj_t *main_button_label = lv_label_create(main_menu_buttons[i]);
            lv_label_set_text(main_button_label, main_menu_names[i]);
            lv_obj_set_style_text_color(main_button_label, lv_color_white(),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_font(main_button_label, &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_center(main_button_label);
        }
    }

#if 0
    /* Old eager page templates retained temporarily for source comparison.
       The live pages are constructed by the cache factories above. */
    lv_obj_t *title = lv_label_create(settings_screen);
    lv_label_set_text(title, "Adjust Time / Date");
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

    for (uint8_t i = 0U; i < 6U; i++)
    {
        settings_fields[i] = lv_btn_create(settings_screen);
        lv_obj_set_size(settings_fields[i], 430, 88);
        lv_obj_align(settings_fields[i], LV_ALIGN_TOP_MID, 0, 78 + (i * 112));
        lv_obj_set_style_radius(settings_fields[i], 10, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(settings_fields[i], &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_add_event_cb(settings_fields[i], ui_button_event, LV_EVENT_ALL, NULL);
        settings_values[i] = lv_label_create(settings_fields[i]);
        lv_obj_set_style_text_font(settings_values[i], &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_label_set_text(settings_values[i], field_names[i]);
        lv_obj_center(settings_values[i]);
    }

    {
        static const char *dac_field_names[5] =
            {"Waveform", "Vpp", "Duty", "Frequency", "Output"};

        lv_obj_t *dac_title = lv_label_create(dac_screen);
        lv_label_set_text(dac_title, "DAC Waveform Output");
        lv_obj_set_style_text_color(dac_title, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(dac_title, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(dac_title, LV_ALIGN_TOP_MID, 0, 10);

        for (uint8_t i = 0U; i < 5U; i++)
        {
            dac_fields[i] = lv_btn_create(dac_screen);
            lv_obj_set_size(dac_fields[i], 430, 88);
            lv_obj_align(dac_fields[i], LV_ALIGN_TOP_MID, 0, 78 + (i * 112));
            lv_obj_set_style_radius(dac_fields[i], 10,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_font(dac_fields[i], &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_add_event_cb(dac_fields[i], ui_button_event, LV_EVENT_ALL, NULL);
            dac_values[i] = lv_label_create(dac_fields[i]);
            lv_obj_set_style_text_font(dac_values[i], &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_label_set_text(dac_values[i], dac_field_names[i]);
            lv_obj_center(dac_values[i]);
        }

        /* Frequency is rendered as separate, fixed-position digit labels.
           The selection box is therefore attached to a real digit position,
           not to an estimated character column in a formatted string. */
        lv_obj_del(dac_values[3]);
        {
            lv_obj_t *dac_frequency_row = lv_obj_create(dac_fields[3]);
            lv_obj_set_size(dac_frequency_row, 300, 38);
            lv_obj_align(dac_frequency_row, LV_ALIGN_CENTER, 0, 0);
            lv_obj_set_style_bg_opa(dac_frequency_row, LV_OPA_TRANSP,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_width(dac_frequency_row, 0,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_pad_all(dac_frequency_row, 0,
                                     LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_clear_flag(dac_frequency_row, LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_clear_flag(dac_frequency_row, LV_OBJ_FLAG_CLICKABLE);

            dac_values[3] = lv_label_create(dac_frequency_row);
            lv_label_set_text(dac_values[3], "Frequency");
            lv_obj_set_style_text_font(dac_values[3], &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_align(dac_values[3], LV_ALIGN_LEFT_MID, 10, 0);

            for (uint8_t i = 0U; i < 5U; i++)
            {
                static const lv_coord_t digit_x[5] = {150, 169, 188, 207, 235};

                dac_frequency_digits[i] = lv_label_create(dac_frequency_row);
                lv_obj_set_width(dac_frequency_digits[i], 18);
                lv_obj_align(dac_frequency_digits[i], LV_ALIGN_LEFT_MID,
                             digit_x[i], 0);
                lv_obj_set_style_text_align(dac_frequency_digits[i], LV_TEXT_ALIGN_CENTER,
                                            LV_PART_MAIN | LV_STATE_DEFAULT);
                lv_obj_set_style_text_font(dac_frequency_digits[i], &lv_font_montserrat_24,
                                           LV_PART_MAIN | LV_STATE_DEFAULT);
                lv_obj_set_style_text_color(dac_frequency_digits[i], lv_color_hex(0xF0F4FA),
                                            LV_PART_MAIN | LV_STATE_DEFAULT);
                lv_label_set_text(dac_frequency_digits[i], "0");
            }

            dac_frequency_dot = lv_label_create(dac_frequency_row);
            lv_label_set_text(dac_frequency_dot, ".");
            lv_obj_align(dac_frequency_dot, LV_ALIGN_LEFT_MID, 226, 0);
            lv_obj_set_style_text_font(dac_frequency_dot, &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_color(dac_frequency_dot, lv_color_hex(0xF0F4FA),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);

            dac_frequency_unit = lv_label_create(dac_frequency_row);
            lv_label_set_text(dac_frequency_unit, "Hz");
            lv_obj_align(dac_frequency_unit, LV_ALIGN_LEFT_MID, 257, 0);
            lv_obj_set_style_text_font(dac_frequency_unit, &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_color(dac_frequency_unit, lv_color_hex(0xF0F4FA),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);

            dac_frequency_box = lv_obj_create(dac_frequency_row);
            lv_obj_set_size(dac_frequency_box, 20, 32);
            lv_obj_set_style_bg_opa(dac_frequency_box, LV_OPA_TRANSP,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_color(dac_frequency_box, lv_color_white(),
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_width(dac_frequency_box, 2,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_radius(dac_frequency_box, 3,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_clear_flag(dac_frequency_box, LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_clear_flag(dac_frequency_box, LV_OBJ_FLAG_CLICKABLE);
            lv_obj_add_flag(dac_frequency_box, LV_OBJ_FLAG_HIDDEN);
        }
    }

    {
        lv_obj_t *adc_title = lv_label_create(adc_screen);
        lv_label_set_text(adc_title, "ADC Scope");
        lv_obj_set_style_text_color(adc_title, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(adc_title, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(adc_title, LV_ALIGN_TOP_MID, 0, 10);

        adc_frequency_label = lv_label_create(adc_screen);
        lv_label_set_text(adc_frequency_label, "Freq: --");
        lv_obj_set_style_text_color(adc_frequency_label, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(adc_frequency_label, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(adc_frequency_label, LV_ALIGN_TOP_MID, 0, 50);

        adc_vpp_label = lv_label_create(adc_screen);
        lv_label_set_text(adc_vpp_label, "Vpp: --");
        lv_obj_set_style_text_color(adc_vpp_label, lv_color_hex(0xA9D5FF),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(adc_vpp_label, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(adc_vpp_label, LV_ALIGN_TOP_MID, 0, 82);

        adc_wave_label = lv_label_create(adc_screen);
        lv_label_set_text(adc_wave_label, "Wave: --");
        lv_obj_set_style_text_color(adc_wave_label, lv_color_hex(0xA9D5FF),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(adc_wave_label, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(adc_wave_label, LV_ALIGN_TOP_MID, 0, 114);

        adc_scope_panel = lv_obj_create(adc_screen);
        lv_obj_set_size(adc_scope_panel, 430, 250);
        lv_obj_align(adc_scope_panel, LV_ALIGN_TOP_MID, 0, 155);
        lv_obj_clear_flag(adc_scope_panel, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_radius(adc_scope_panel, 10,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(adc_scope_panel, panel_bg,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(adc_scope_panel, panel_border,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(adc_scope_panel, 2,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_pad_all(adc_scope_panel, 0,
                                 LV_PART_MAIN | LV_STATE_DEFAULT);

        adc_scope_line = lv_line_create(adc_scope_panel);
        lv_obj_set_style_line_color(adc_scope_line, lv_color_hex(0x49D17D),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_line_width(adc_scope_line, 2,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_line_rounded(adc_scope_line, true,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);

        adc_back_button = lv_btn_create(adc_screen);
        lv_obj_set_size(adc_back_button, 260, 64);
        lv_obj_align(adc_back_button, LV_ALIGN_TOP_MID, 0, 430);
        lv_obj_set_style_radius(adc_back_button, 10,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(adc_back_button, panel_bg,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(adc_back_button, panel_bg,
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(adc_back_button, panel_border,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(adc_back_button, 2,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(adc_back_button, lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(adc_back_button, 5,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_add_event_cb(adc_back_button, ui_button_event, LV_EVENT_ALL, NULL);

        lv_obj_t *adc_back_label = lv_label_create(adc_back_button);
        lv_label_set_text(adc_back_label, "UP: Back");
        lv_obj_set_style_text_color(adc_back_label, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(adc_back_label, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_center(adc_back_label);
    }

    {
        lv_obj_t *alarm_title = lv_label_create(alarm_screen);
        lv_label_set_text(alarm_title, "Alarm");
        lv_obj_set_style_text_color(alarm_title, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(alarm_title, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(alarm_title, LV_ALIGN_TOP_MID, 0, 20);

        for (uint8_t i = 0U; i < 3U; i++)
        {
            alarm_menu_buttons[i] = lv_btn_create(alarm_screen);
            lv_obj_set_size(alarm_menu_buttons[i], 430, 100);
            lv_obj_align(alarm_menu_buttons[i], LV_ALIGN_TOP_MID, 0, 85 + i * 125);
            lv_obj_set_style_radius(alarm_menu_buttons[i], 10,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_bg_color(alarm_menu_buttons[i], panel_bg,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_bg_color(alarm_menu_buttons[i], panel_bg,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_set_style_border_color(alarm_menu_buttons[i], panel_border,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_width(alarm_menu_buttons[i], 2,
                                          LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_border_color(alarm_menu_buttons[i], lv_color_white(),
                                          LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_set_style_border_width(alarm_menu_buttons[i], 5,
                                          LV_PART_MAIN | LV_STATE_FOCUSED);
            lv_obj_add_event_cb(alarm_menu_buttons[i], ui_button_event,
                                LV_EVENT_ALL, NULL);

            alarm_menu_values[i] = lv_label_create(alarm_menu_buttons[i]);
            lv_obj_set_style_text_color(alarm_menu_values[i], lv_color_hex(0xF0F4FA),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_font(alarm_menu_values[i], &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_align(alarm_menu_values[i], LV_ALIGN_LEFT_MID, 24, 0);
        }

        alarm_chime_indicator = lv_obj_create(alarm_menu_buttons[0]);
        lv_obj_set_size(alarm_chime_indicator, 34, 34);
        lv_obj_align(alarm_chime_indicator, LV_ALIGN_RIGHT_MID, -24, 0);
        lv_obj_clear_flag(alarm_chime_indicator, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(alarm_chime_indicator, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_radius(alarm_chime_indicator, LV_RADIUS_CIRCLE,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        ui_update_round_toggle(alarm_chime_indicator, 0U);
    }

    {
        alarm_edit_title = lv_label_create(alarm_edit_screen);
        lv_obj_set_style_text_color(alarm_edit_title, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(alarm_edit_title, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_label_set_text(alarm_edit_title, "Adjust Alarm");
        lv_obj_align(alarm_edit_title, LV_ALIGN_TOP_MID, 0, 20);

        for (uint8_t i = 0U; i < 4U; i++)
        {
            alarm_edit_fields[i] = lv_btn_create(alarm_edit_screen);
            lv_obj_set_size(alarm_edit_fields[i], 430, 105);
            lv_obj_align(alarm_edit_fields[i], LV_ALIGN_TOP_MID, 0, 85 + i * 120);
            lv_obj_set_style_radius(alarm_edit_fields[i], 10,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_add_event_cb(alarm_edit_fields[i], ui_button_event,
                                LV_EVENT_ALL, NULL);
            alarm_edit_values[i] = lv_label_create(alarm_edit_fields[i]);
            lv_obj_set_style_text_font(alarm_edit_values[i], &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_align(alarm_edit_values[i], LV_ALIGN_LEFT_MID, 24, 0);
        }

        alarm_edit_indicator = lv_obj_create(alarm_edit_fields[3]);
        lv_obj_set_size(alarm_edit_indicator, 34, 34);
        lv_obj_align(alarm_edit_indicator, LV_ALIGN_RIGHT_MID, -24, 0);
        lv_obj_clear_flag(alarm_edit_indicator, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_clear_flag(alarm_edit_indicator, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_radius(alarm_edit_indicator, LV_RADIUS_CIRCLE,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        ui_update_round_toggle(alarm_edit_indicator, 0U);
    }

#endif

    {
        alarm_popup_title = lv_label_create(alarm_popup_screen);
        lv_obj_set_style_text_color(alarm_popup_title, lv_color_hex(0xFF5C70),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(alarm_popup_title, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(alarm_popup_title, LV_ALIGN_TOP_MID, 0, 165);

        alarm_popup_hint = lv_label_create(alarm_popup_screen);
        lv_obj_set_style_text_color(alarm_popup_hint, lv_color_white(),
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_font(alarm_popup_hint, &lv_font_montserrat_24,
                                   LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_text_align(alarm_popup_hint, LV_TEXT_ALIGN_CENTER,
                                    LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_align(alarm_popup_hint, LV_ALIGN_TOP_MID, 0, 245);

        alarm_popup_close_button = lv_btn_create(alarm_popup_screen);
        lv_obj_set_size(alarm_popup_close_button, 330, 100);
        lv_obj_align(alarm_popup_close_button, LV_ALIGN_TOP_MID, 0, 390);
        lv_obj_set_style_radius(alarm_popup_close_button, 10,
                                LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(alarm_popup_close_button, panel_bg,
                                  LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(alarm_popup_close_button, panel_bg,
                                  LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_color(alarm_popup_close_button, lv_color_hex(0xFF5C70),
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_width(alarm_popup_close_button, 3,
                                      LV_PART_MAIN | LV_STATE_DEFAULT);
        lv_obj_set_style_border_color(alarm_popup_close_button, lv_color_white(),
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(alarm_popup_close_button, 6,
                                      LV_PART_MAIN | LV_STATE_FOCUSED);
        lv_obj_add_event_cb(alarm_popup_close_button, ui_button_event,
                            LV_EVENT_ALL, NULL);
        {
            lv_obj_t *close_label = lv_label_create(alarm_popup_close_button);
            lv_label_set_text(close_label, "KEY1: STOP");
            lv_obj_set_style_text_color(close_label, lv_color_white(),
                                        LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_set_style_text_font(close_label, &lv_font_montserrat_24,
                                       LV_PART_MAIN | LV_STATE_DEFAULT);
            lv_obj_center(close_label);
        }
    }

    ui_group = lv_group_create();
    lv_group_set_default(ui_group);
    lv_indev_drv_init(&lvgl_indev_drv);
    lvgl_indev_drv.type = LV_INDEV_TYPE_KEYPAD;
    lvgl_indev_drv.read_cb = lvgl_keypad_read;
    keypad_indev = lv_indev_drv_register(&lvgl_indev_drv);
    lv_indev_set_group(keypad_indev, ui_group);

    ui_read_rtc();
    ui_show_main();
}

void BSP_LVGL_Init(void)
{
    lv_init();

#if (LVGL_DIAG_INIT_STAGE == 1U)
    /* DIAG: The remaining display/UI setup is intentionally bypassed. */
    return;
#endif

    lv_disp_draw_buf_init(&lvgl_draw_buf,
                          lvgl_draw_buf_mem,
                          NULL,
                          LVGL_HOR_RES * LVGL_BUF_LINES);

    lv_disp_drv_init(&lvgl_disp_drv);
    lvgl_disp_drv.hor_res = LVGL_HOR_RES;
    lvgl_disp_drv.ver_res = LVGL_VER_RES;
    lvgl_disp_drv.flush_cb = lvgl_flush_cb;
    lvgl_disp_drv.draw_buf = &lvgl_draw_buf;
    (void)lv_disp_drv_register(&lvgl_disp_drv);

#if (LVGL_DIAG_INIT_STAGE == 2U)
    /* DIAG: UI object creation is intentionally bypassed. */
    return;
#endif

    lvgl_started = 1U;
    ui_create();
}

void BSP_LVGL_TickInc(void)
{
    /* LVGL obtains time directly from HAL_GetTick(). */
}

void BSP_LVGL_Task(void *argument)
{
    uint32_t last_clock_update = 0U;
    uint32_t last_adc_update = 0U;
    (void)argument;

    for (;;)
    {
#if (LVGL_DIAG_SKIP_TIMER_HANDLER == 0U)
        lv_timer_handler();
#endif

        uint32_t now = lv_tick_get();

        /* RTC interrupt callbacks only set a one-byte pending flag.  The
           display transition and the buzzer state machine both run here in
           thread context, so neither blocks an interrupt or LVGL itself. */
        if (ui_alarm_ringing == 0U)
        {
            uint8_t alarm_id;
            if (BSP_Alarm_TakePending(&alarm_id) != 0U)
                ui_alarm_start(alarm_id, now);
        }
        else
        {
            ui_alarm_buzzer_update(now);
            if ((uint32_t)(now - ui_alarm_started_tick) >= 30000U)
                ui_alarm_finish(2U);
        }

        if ((uint32_t)(now - last_clock_update) >= 100U)
        {
            last_clock_update = now;
            ui_update_main_clock();
        }

        /* ADC data is acquired continuously by DMA.  Redraw the scope only
           while its page is visible, at 4 fps, and only inside its line area. */
        if ((ui_page == UI_PAGE_ADC) &&
            ((uint32_t)(now - last_adc_update) >= 250U))
        {
            last_adc_update = now;
            ui_update_adc_scope();
        }

        osDelay(5U);
    }
}
