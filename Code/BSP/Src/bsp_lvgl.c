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

static uint8_t ui_is_editing(void)
{
    return (ui_page == UI_PAGE_DAC) ?
           (uint8_t)((ui_dac_editing != 0U) ||
                     (ui_dac_selecting_frequency_digit != 0U)) :
           ui_editing;
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
                /* Alarm and Game are deliberate menu placeholders for now. */
                return;
            }
        }

        if (target == adc_back_button)
        {
            ui_show_main();
            return;
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
            if (ui_page == UI_PAGE_DAC)
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

static void ui_create(void)
{
    static const char *field_names[6] =
        {"Hour", "Minute", "Second", "Year", "Month", "Date"};
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

    settings_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(settings_screen, page_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(settings_screen, LV_OPA_COVER, LV_PART_MAIN | LV_STATE_DEFAULT);

    dac_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(dac_screen, page_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(dac_screen, LV_OPA_COVER, LV_PART_MAIN | LV_STATE_DEFAULT);

    adc_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(adc_screen, page_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_opa(adc_screen, LV_OPA_COVER, LV_PART_MAIN | LV_STATE_DEFAULT);

#if (LVGL_DIAG_UI_STAGE == 2U)
    /* DIAG: Widget creation is intentionally bypassed. */
    return;
#endif

    main_clock_label = lv_label_create(main_screen);
    lv_obj_set_style_text_color(main_clock_label, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(main_clock_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_align(main_clock_label, LV_ALIGN_TOP_MID, 0, 20);

    clock_face = lv_obj_create(main_screen);
    lv_obj_set_size(clock_face, CLOCK_DIAL_SIZE, CLOCK_DIAL_SIZE);
    lv_obj_align(clock_face, LV_ALIGN_TOP_MID, 0, 120);
    lv_obj_clear_flag(clock_face, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(clock_face, LV_RADIUS_CIRCLE, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(clock_face, panel_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(clock_face, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(clock_face, 4, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_pad_all(clock_face, 0, LV_PART_MAIN | LV_STATE_DEFAULT);

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
