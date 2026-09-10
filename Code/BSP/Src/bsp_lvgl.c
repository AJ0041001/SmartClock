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
static lv_obj_t *main_settings_button;
static lv_obj_t *clock_face;
static lv_obj_t *clock_hour_hand;
static lv_obj_t *clock_minute_hand;
static lv_obj_t *clock_second_hand;
static lv_obj_t *settings_screen;
static lv_obj_t *settings_fields[6];
static lv_obj_t *settings_values[6];

static RTC_TimeTypeDef ui_time;
static RTC_DateTypeDef ui_date;
static uint8_t ui_field;
static uint8_t ui_editing;
static uint32_t ui_last_clock_stamp = 0xFFFFFFFFU;

/* lv_line_set_points() stores the point-array address, so these must have
   static lifetime just like the LVGL driver descriptors. */
static lv_point_t clock_hour_points[2];
static lv_point_t clock_minute_points[2];
static lv_point_t clock_second_points[2];

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
    ui_editing = 0U;
    lv_scr_load(main_screen);
    lv_group_remove_all_objs(ui_group);
    lv_group_add_obj(ui_group, main_settings_button);
    lv_group_focus_obj(main_settings_button);
    ui_update_main_clock();
}

static void ui_show_settings(void)
{
    ui_editing = 0U;
    ui_read_rtc();
    lv_scr_load(settings_screen);
    lv_group_remove_all_objs(ui_group);

    for (uint8_t i = 0U; i < 6U; i++)
        lv_group_add_obj(ui_group, settings_fields[i]);

    lv_group_focus_obj(settings_fields[0]);
    ui_update_settings_text();
    ui_update_edit_appearance();
}

static void ui_button_event(lv_event_t *event)
{
    lv_event_code_t code = lv_event_get_code(event);
    lv_obj_t *target = lv_event_get_target(event);

    if (code == LV_EVENT_CLICKED)
    {
        if (target == main_settings_button)
        {
            ui_show_settings();
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
    }

    if (code == LV_EVENT_KEY)
    {
        uint32_t key = lv_event_get_key(event);

        if (key == LV_KEY_ESC)
        {
            if (ui_editing != 0U)
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
        else if (ui_editing != 0U && key == LV_KEY_LEFT)
        {
            ui_adjust_value(-1);
            ui_update_edit_appearance();
        }
        else if (ui_editing != 0U && key == LV_KEY_RIGHT)
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
        case 1U: return (ui_editing != 0U) ? LV_KEY_RIGHT : LV_KEY_NEXT;
        case 2U: return LV_KEY_ENTER;
        case 3U: return (ui_editing != 0U) ? LV_KEY_LEFT : LV_KEY_PREV;
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

    main_settings_button = lv_btn_create(main_screen);
    lv_obj_set_size(main_settings_button, 430, 88);
    lv_obj_align(main_settings_button, LV_ALIGN_CENTER, 0, 150);
    lv_obj_set_style_radius(main_settings_button, 10, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_bg_color(main_settings_button, panel_bg, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(main_settings_button, panel_border, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_width(main_settings_button, 2, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_border_color(main_settings_button, lv_color_white(), LV_PART_MAIN | LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(main_settings_button, 5, LV_PART_MAIN | LV_STATE_FOCUSED);
    lv_obj_add_event_cb(main_settings_button, ui_button_event, LV_EVENT_ALL, NULL);
    lv_obj_t *main_button_label = lv_label_create(main_settings_button);
    lv_label_set_text(main_button_label, "Time Settings");
    lv_obj_set_style_text_color(main_button_label, lv_color_white(), LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_set_style_text_font(main_button_label, &lv_font_montserrat_24, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_obj_center(main_button_label);

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

        osDelay(5U);
    }
}
