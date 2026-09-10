#include "../Inc/bsp_board.h"
#include "main.h"
#include "delay.h"
#include "bsp_touch.h"
#include "wm8978.h"
#include "lcd.h"
#include "fatfs.h"
#include <string.h>

extern ADC_HandleTypeDef hadc1;
extern DAC_HandleTypeDef hdac;
extern I2S_HandleTypeDef hi2s2;
extern TIM_HandleTypeDef htim3;
extern TIM_HandleTypeDef htim6;
extern UART_HandleTypeDef huart1;
extern RTC_HandleTypeDef hrtc;

#define BSP_ADC_CAPTURE_SAMPLE_COUNT 4096U
#define BSP_ADC_CAPTURE_MASK         (BSP_ADC_CAPTURE_SAMPLE_COUNT - 1U)
#define BSP_ADC_MIN_SPAN_CODES       40U
#define BSP_ADC_FAST_SAMPLE_RATE_HZ  80000U
#define BSP_ADC_MID_SAMPLE_RATE_HZ   20000U
#define BSP_ADC_LOW_SAMPLE_RATE_HZ   1600U
#define BSP_ADC_MID_ENTER_HZ_X10     1500U
#define BSP_ADC_FAST_RETURN_HZ_X10   2500U
#define BSP_ADC_LOW_ENTER_HZ_X10     200U
#define BSP_ADC_MID_RETURN_HZ_X10    250U
#define BSP_ADC_LOW_TIMEBASE_HZ_X10  500U

static volatile uint16_t adc_samples[BSP_ADC_CAPTURE_SAMPLE_COUNT];
static uint16_t dac_samples[128];
static uint16_t audio_samples[128];
static uint8_t adc_started;
static uint8_t audio_started;
static uint32_t adc_start_tick;
static uint32_t adc_frequency_history[4];
static uint32_t adc_frequency_sum;
static uint8_t adc_frequency_count;
static uint8_t adc_frequency_index;

/* Alarm configuration deliberately lives outside CubeMX-generated files.
   CubeMX can be regenerated without replacing this user-facing behaviour. */
static BspAlarmConfig bsp_alarm_config[2] = {
  {7U, 0U, 0U, 0U},
  {7U, 30U, 0U, 0U}
};
static volatile uint8_t bsp_alarm_pending_mask;
static uint8_t bsp_alarm_snoozed[2];

#define BSP_DAC_SAMPLE_COUNT 128U
#define BSP_DAC_VREF_X10     33U
#define BSP_DAC_MID_CODE     2048U

/* One complete cycle is kept in the DMA buffer.  TIM6 is then configured
   for waveform_frequency * BSP_DAC_SAMPLE_COUNT update events per second. */
static const int16_t bsp_dac_sine_64[64] = {
      0,  98, 195, 290, 383, 471, 556, 634,
    707, 773, 831, 882, 924, 957, 981, 995,
   1000, 995, 981, 957, 924, 882, 831, 773,
    707, 634, 556, 471, 383, 290, 195,  98,
      0, -98,-195,-290,-383,-471,-556,-634,
   -707,-773,-831,-882,-924,-957,-981,-995,
  -1000,-995,-981,-957,-924,-882,-831,-773,
   -707,-634,-556,-471,-383,-290,-195, -98
};

static uint32_t bsp_dac_timer_clock(void)
{
  RCC_ClkInitTypeDef clock_config;
  uint32_t flash_latency;
  uint32_t timer_clock = HAL_RCC_GetPCLK1Freq();

  HAL_RCC_GetClockConfig(&clock_config, &flash_latency);
  if (clock_config.APB1CLKDivider != RCC_HCLK_DIV1) timer_clock *= 2U;
  return timer_clock;
}

static void bsp_dac_configure_timer(uint16_t frequency_x10)
{
  uint32_t timer_clock = bsp_dac_timer_clock();
  uint32_t target_rate_x10 = (uint32_t)frequency_x10 * BSP_DAC_SAMPLE_COUNT;
  uint32_t period_ticks;
  uint32_t prescaler = 0U;
  uint32_t period;

  /* frequency_x10 is non-zero here.  Multiplication by ten retains the
     requested 0.1 Hz resolution without using floating point. */
  period_ticks = (timer_clock * 10U + (target_rate_x10 / 2U)) /
                 target_rate_x10;
  if (period_ticks > 65536U)
  {
    prescaler = (period_ticks + 65535U) / 65536U - 1U;
    if (prescaler > 65535U) prescaler = 65535U;
  }

  period = (period_ticks + ((prescaler + 1U) / 2U)) /
           (prescaler + 1U);
  if (period < 1U) period = 1U;
  if (period > 65536U) period = 65536U;

  __HAL_TIM_SET_PRESCALER(&htim6, prescaler);
  __HAL_TIM_SET_AUTORELOAD(&htim6, period - 1U);
  __HAL_TIM_SET_COUNTER(&htim6, 0U);
}

static uint16_t bsp_dac_wave_sample(const BspDacConfig *config,
                                    uint32_t index,
                                    uint32_t low_code,
                                    uint32_t high_code)
{
  int32_t wave = 0;
  uint32_t threshold;
  uint32_t code;

  switch (config->waveform)
  {
    case BSP_DAC_WAVE_SQUARE:
      threshold = ((uint32_t)config->duty_percent * BSP_DAC_SAMPLE_COUNT + 99U) / 100U;
      wave = (index < threshold) ? 1000 : -1000;
      break;

    case BSP_DAC_WAVE_TRIANGLE:
      if (index < (BSP_DAC_SAMPLE_COUNT / 2U))
        wave = -1000 + (int32_t)(index * 2000U / 63U);
      else
        wave = 1000 - (int32_t)((index - 64U) * 2000U / 63U);
      break;

    case BSP_DAC_WAVE_SAWTOOTH:
      wave = -1000 + (int32_t)(index * 2000U / 127U);
      break;

    case BSP_DAC_WAVE_SINE:
    default:
      wave = bsp_dac_sine_64[index / 2U];
      break;
  }

  if (wave <= -1000) return (uint16_t)low_code;
  if (wave >= 1000) return (uint16_t)high_code;

  code = low_code + (uint32_t)(((int32_t)(wave + 1000) *
                                (int32_t)(high_code - low_code)) / 2000);
  if (code > 4095U) code = 4095U;
  return (uint16_t)code;
}

void BSP_Log(const char *text)
{
  if (text != NULL) HAL_UART_Transmit(&huart1, (uint8_t *)text, (uint16_t)strlen(text), 100U);
}

void BSP_LED_Set(uint8_t led, uint8_t on)
{
  GPIO_TypeDef *port = GPIOF;
  uint16_t pin = (led == 0U) ? LED0_Pin : LED1_Pin;
  HAL_GPIO_WritePin(port, pin, on ? GPIO_PIN_RESET : GPIO_PIN_SET);
}

void BSP_Buzzer_Set(uint8_t on)
{
  HAL_GPIO_WritePin(BEEP_GPIO_Port, BEEP_Pin,
                    (on != 0U) ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void BSP_Buzzer_Beep(uint32_t milliseconds)
{
  BSP_Buzzer_Set(1U);
  HAL_Delay(milliseconds);
  BSP_Buzzer_Set(0U);
}

static uint8_t bsp_alarm_is_valid(uint8_t alarm_id)
{
  return (uint8_t)(alarm_id <= BSP_ALARM_B);
}

static uint8_t bsp_alarm_days_in_month(uint8_t year, uint8_t month)
{
  static const uint8_t days[] =
      {31U, 28U, 31U, 30U, 31U, 30U, 31U, 31U, 30U, 31U, 30U, 31U};
  uint16_t full_year = (uint16_t)year + 2000U;

  if (month == 2U && ((full_year % 4U) == 0U)) return 29U;
  if (month < 1U || month > 12U) return 31U;
  return days[month - 1U];
}

static uint32_t bsp_alarm_hal_id(uint8_t alarm_id)
{
  return (alarm_id == BSP_ALARM_A) ? RTC_ALARM_A : RTC_ALARM_B;
}

static void bsp_alarm_deactivate(uint8_t alarm_id)
{
  (void)HAL_RTC_DeactivateAlarm(&hrtc, bsp_alarm_hal_id(alarm_id));
}

static uint8_t bsp_alarm_program_daily(uint8_t alarm_id)
{
  RTC_AlarmTypeDef alarm = {0};
  const BspAlarmConfig *config;

  if (bsp_alarm_is_valid(alarm_id) == 0U) return 0U;
  config = &bsp_alarm_config[alarm_id];
  bsp_alarm_deactivate(alarm_id);
  if (config->enabled == 0U) return 1U;

  alarm.AlarmTime.Hours = config->hours;
  alarm.AlarmTime.Minutes = config->minutes;
  alarm.AlarmTime.Seconds = config->seconds;
  alarm.AlarmTime.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
  alarm.AlarmTime.StoreOperation = RTC_STOREOPERATION_RESET;
  /* Masking the date makes the alarm recur every day at the configured time. */
  alarm.AlarmMask = RTC_ALARMMASK_DATEWEEKDAY;
  alarm.AlarmSubSecondMask = RTC_ALARMSUBSECONDMASK_ALL;
  alarm.AlarmDateWeekDaySel = RTC_ALARMDATEWEEKDAYSEL_DATE;
  alarm.AlarmDateWeekDay = 1U;
  alarm.Alarm = bsp_alarm_hal_id(alarm_id);

  return (HAL_RTC_SetAlarm_IT(&hrtc, &alarm, RTC_FORMAT_BIN) == HAL_OK) ? 1U : 0U;
}

static uint8_t bsp_alarm_program_snooze(uint8_t alarm_id)
{
  RTC_TimeTypeDef time = {0};
  RTC_DateTypeDef date = {0};
  RTC_AlarmTypeDef alarm = {0};

  if (bsp_alarm_is_valid(alarm_id) == 0U) return 0U;
  (void)HAL_RTC_GetTime(&hrtc, &time, RTC_FORMAT_BIN);
  (void)HAL_RTC_GetDate(&hrtc, &date, RTC_FORMAT_BIN);

  time.Minutes = (uint8_t)(time.Minutes + 5U);
  if (time.Minutes >= 60U)
  {
    time.Minutes = (uint8_t)(time.Minutes - 60U);
    time.Hours++;
    if (time.Hours >= 24U)
    {
      time.Hours = 0U;
      date.Date++;
      if (date.Date > bsp_alarm_days_in_month(date.Year, date.Month))
      {
        date.Date = 1U;
        date.Month++;
        if (date.Month > 12U)
        {
          date.Month = 1U;
          date.Year = (uint8_t)((date.Year + 1U) % 100U);
        }
      }
    }
  }

  bsp_alarm_deactivate(alarm_id);
  alarm.AlarmTime.Hours = time.Hours;
  alarm.AlarmTime.Minutes = time.Minutes;
  alarm.AlarmTime.Seconds = time.Seconds;
  alarm.AlarmTime.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
  alarm.AlarmTime.StoreOperation = RTC_STOREOPERATION_RESET;
  /* Date is intentionally not masked: a snooze is a one-shot alarm. */
  alarm.AlarmMask = RTC_ALARMMASK_NONE;
  alarm.AlarmSubSecondMask = RTC_ALARMSUBSECONDMASK_ALL;
  alarm.AlarmDateWeekDaySel = RTC_ALARMDATEWEEKDAYSEL_DATE;
  alarm.AlarmDateWeekDay = date.Date;
  alarm.Alarm = bsp_alarm_hal_id(alarm_id);

  return (HAL_RTC_SetAlarm_IT(&hrtc, &alarm, RTC_FORMAT_BIN) == HAL_OK) ? 1U : 0U;
}

void BSP_Alarm_Init(void)
{
  /* CubeMX emits two demonstration alarms.  Both must be removed before
     user configuration is applied; otherwise a hidden 00:00 alarm exists. */
  bsp_alarm_pending_mask = 0U;
  for (uint8_t i = BSP_ALARM_A; i <= BSP_ALARM_B; i++)
  {
    bsp_alarm_snoozed[i] = 0U;
    (void)bsp_alarm_program_daily(i);
  }
}

void BSP_Alarm_Get(uint8_t alarm_id, BspAlarmConfig *config)
{
  if (config == NULL || bsp_alarm_is_valid(alarm_id) == 0U) return;
  *config = bsp_alarm_config[alarm_id];
}

uint8_t BSP_Alarm_Set(uint8_t alarm_id, const BspAlarmConfig *config)
{
  BspAlarmConfig safe_config;

  if (config == NULL || bsp_alarm_is_valid(alarm_id) == 0U) return 0U;
  safe_config = *config;
  if (safe_config.hours > 23U) safe_config.hours = 23U;
  if (safe_config.minutes > 59U) safe_config.minutes = 59U;
  if (safe_config.seconds > 59U) safe_config.seconds = 59U;
  safe_config.enabled = (safe_config.enabled != 0U) ? 1U : 0U;
  bsp_alarm_config[alarm_id] = safe_config;
  bsp_alarm_snoozed[alarm_id] = 0U;
  return bsp_alarm_program_daily(alarm_id);
}

uint8_t BSP_Alarm_TakePending(uint8_t *alarm_id)
{
  uint8_t pending = bsp_alarm_pending_mask;

  if ((pending & 0x01U) != 0U)
  {
    bsp_alarm_pending_mask = (uint8_t)(pending & (uint8_t)~0x01U);
    if (alarm_id != NULL) *alarm_id = BSP_ALARM_A;
    return 1U;
  }
  if ((pending & 0x02U) != 0U)
  {
    bsp_alarm_pending_mask = (uint8_t)(pending & (uint8_t)~0x02U);
    if (alarm_id != NULL) *alarm_id = BSP_ALARM_B;
    return 1U;
  }
  return 0U;
}

void BSP_Alarm_Snooze(uint8_t alarm_id)
{
  if (bsp_alarm_is_valid(alarm_id) == 0U) return;
  if (bsp_alarm_program_snooze(alarm_id) != 0U) bsp_alarm_snoozed[alarm_id] = 1U;
}

void BSP_Alarm_Dismiss(uint8_t alarm_id)
{
  if (bsp_alarm_is_valid(alarm_id) == 0U) return;
  bsp_alarm_snoozed[alarm_id] = 0U;
  (void)bsp_alarm_program_daily(alarm_id);
}

void BSP_Alarm_Timeout(uint8_t alarm_id)
{
  if (bsp_alarm_is_valid(alarm_id) == 0U) return;

  /* First unanswered alarm snoozes.  If that snoozed alarm is ignored too,
     disable it completely as requested instead of ringing indefinitely. */
  if (bsp_alarm_snoozed[alarm_id] == 0U)
  {
    BSP_Alarm_Snooze(alarm_id);
  }
  else
  {
    bsp_alarm_config[alarm_id].enabled = 0U;
    bsp_alarm_snoozed[alarm_id] = 0U;
    bsp_alarm_deactivate(alarm_id);
  }
}

void HAL_RTC_AlarmAEventCallback(RTC_HandleTypeDef *rtc)
{
  if (rtc == &hrtc) bsp_alarm_pending_mask |= 0x01U;
}

void HAL_RTCEx_AlarmBEventCallback(RTC_HandleTypeDef *rtc)
{
  if (rtc == &hrtc) bsp_alarm_pending_mask |= 0x02U;
}

uint8_t BSP_Key_Read(void)
{
  if (HAL_GPIO_ReadPin(KEY0_GPIO_Port, KEY0_Pin) == GPIO_PIN_RESET) return 1U;
  if (HAL_GPIO_ReadPin(KEY1_GPIO_Port, KEY1_Pin) == GPIO_PIN_RESET) return 2U;
  if (HAL_GPIO_ReadPin(KEY2_GPIO_Port, KEY2_Pin) == GPIO_PIN_RESET) return 3U;
  if (HAL_GPIO_ReadPin(KEY_UP_GPIO_Port, KEY_UP_Pin) == GPIO_PIN_SET) return 4U;
  return 0U;
}

static uint8_t bsp_adc_start_if_needed(void)
{
  if (adc_started != 0U) return 1U;

  if (HAL_ADC_Start_DMA(&hadc1, (uint32_t *)adc_samples,
                        BSP_ADC_CAPTURE_SAMPLE_COUNT) != HAL_OK)
    return 0U;

  adc_started = 1U;
  adc_start_tick = HAL_GetTick();
  return 1U;
}

static uint32_t bsp_adc_sample_rate_hz(void)
{
  /* TIM3 and TIM6 both belong to APB1.  Read the actual TIM3 dividers so
     this remains correct if CubeMX later changes the sampling timer setup. */
  uint32_t timer_clock = bsp_dac_timer_clock();
  uint32_t prescaler = (uint32_t)htim3.Instance->PSC + 1U;
  uint32_t period = (uint32_t)htim3.Instance->ARR + 1U;

  return timer_clock / prescaler / period;
}

static uint32_t bsp_adc_filter_frequency(uint32_t raw_frequency_x10);

/* Low frequencies need a longer time window; high frequencies need more
   points per period.  TIM3 is changed only at runtime and all measurement
   code reads its actual register values, so CubeMX can keep its 80 kHz
   startup configuration. */
static void bsp_adc_set_sample_rate(uint32_t target_rate_hz)
{
  uint32_t timer_clock;
  uint32_t period_ticks;

  if (bsp_adc_sample_rate_hz() == target_rate_hz) return;

  timer_clock = bsp_dac_timer_clock();
  period_ticks = (timer_clock + target_rate_hz / 2U) / target_rate_hz;
  if (period_ticks < 1U) period_ticks = 1U;
  if (period_ticks > 65536U) period_ticks = 65536U;

  (void)HAL_TIM_Base_Stop(&htim3);
  __HAL_TIM_SET_PRESCALER(&htim3, 0U);
  __HAL_TIM_SET_AUTORELOAD(&htim3, period_ticks - 1U);
  __HAL_TIM_SET_COUNTER(&htim3, 0U);
  htim3.Instance->EGR = TIM_EGR_UG;
  (void)HAL_TIM_Base_Start(&htim3);

  /* Let one entire DMA ring be replaced at the new rate before analysing it. */
  adc_start_tick = HAL_GetTick();
  (void)bsp_adc_filter_frequency(0U);
}

static uint16_t bsp_adc_ring_sample(uint16_t start_index, uint16_t offset)
{
  return adc_samples[(start_index + offset) & BSP_ADC_CAPTURE_MASK];
}

/* The raw period estimate is quantized to ADC sample positions.  A four-frame
   average makes the reported value stable while keeping a genuine, large
   frequency change responsive by resetting the short history. */
static uint32_t bsp_adc_filter_frequency(uint32_t raw_frequency_x10)
{
  uint32_t average;
  uint32_t difference;

  if (raw_frequency_x10 == 0U)
  {
    adc_frequency_sum = 0U;
    adc_frequency_count = 0U;
    adc_frequency_index = 0U;
    return 0U;
  }

  if (adc_frequency_count != 0U)
  {
    average = adc_frequency_sum / adc_frequency_count;
    difference = (raw_frequency_x10 >= average) ?
                 (raw_frequency_x10 - average) : (average - raw_frequency_x10);
    if (difference * 5U > average)
    {
      adc_frequency_sum = 0U;
      adc_frequency_count = 0U;
      adc_frequency_index = 0U;
    }
  }

  if (adc_frequency_count < 4U)
  {
    adc_frequency_history[adc_frequency_count] = raw_frequency_x10;
    adc_frequency_sum += raw_frequency_x10;
    adc_frequency_count++;
  }
  else
  {
    adc_frequency_sum -= adc_frequency_history[adc_frequency_index];
    adc_frequency_history[adc_frequency_index] = raw_frequency_x10;
    adc_frequency_sum += raw_frequency_x10;
    adc_frequency_index = (uint8_t)((adc_frequency_index + 1U) % 4U);
  }

  return (adc_frequency_sum + adc_frequency_count / 2U) / adc_frequency_count;
}

/* Make the displayed trace behave like an oscilloscope AUTO timebase.
   Analysis still uses all 4096 samples.  Once frequency is known, display
   roughly five periods, align the trace to a valid rising crossing, and
   retain a small pre-trigger section. */
static void bsp_adc_fill_scope_trace(BspAdcScopeFrame *frame,
                                     uint16_t start_index,
                                     uint32_t sample_rate)
{
  uint32_t view_samples = BSP_ADC_CAPTURE_SAMPLE_COUNT;
  uint32_t trace_start;
  uint8_t timebase_limited = 0U;
  uint16_t low_band = (uint16_t)(frame->minimum +
                      (frame->maximum - frame->minimum) / 8U);
  uint16_t high_band = (uint16_t)(frame->maximum -
                       (frame->maximum - frame->minimum) / 8U);
  uint16_t trigger = 0U;
  uint16_t first_trigger = 0U;
  uint8_t trigger_found = 0U;
  uint8_t first_trigger_found = 0U;
  uint8_t trigger_armed = 0U;
  uint16_t trace[BSP_ADC_SCOPE_POINTS];

  if (frame->frequency_x10 != 0U)
  {
    uint32_t period_samples = (sample_rate * 10U + frame->frequency_x10 / 2U) /
                              frame->frequency_x10;
    uint32_t display_periods = (frame->frequency_x10 <
                                BSP_ADC_LOW_TIMEBASE_HZ_X10) ? 2U : 5U;

    view_samples = period_samples * display_periods;
    if (view_samples < BSP_ADC_SCOPE_POINTS) view_samples = BSP_ADC_SCOPE_POINTS;
    if (view_samples > BSP_ADC_CAPTURE_SAMPLE_COUNT)
    {
      view_samples = BSP_ADC_CAPTURE_SAMPLE_COUNT;
      timebase_limited = 1U;
    }
  }

  /* Use a hysteretic low-to-high trigger rather than a single threshold.
     This prevents a few ADC-noise codes near the midpoint from moving the
     visible waveform left/right between frames. */
  for (uint32_t i = 1U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
  {
    uint16_t current = bsp_adc_ring_sample(start_index, (uint16_t)i);

    if (current <= low_band) trigger_armed = 1U;
    if ((trigger_armed != 0U) && (current >= high_band))
    {
      if (first_trigger_found == 0U)
      {
        first_trigger = (uint16_t)i;
        first_trigger_found = 1U;
      }

      /* If five periods fit, take the latest edge with enough following
         data.  If they do not fit (for example 1 Hz), take the first edge
         and draw the remaining continuous history instead of wrapping or
         mixing two unrelated phases. */
      if ((timebase_limited != 0U) ||
          (i + view_samples <= BSP_ADC_CAPTURE_SAMPLE_COUNT))
      {
        if ((timebase_limited == 0U) || (trigger_found == 0U))
        {
          trigger = (uint16_t)i;
          trigger_found = 1U;
        }
      }
      trigger_armed = 0U;
    }
  }

  /* A low-frequency frame can contain fewer than the requested display
     periods after its first edge.  Keep that real edge and shorten only the
     drawn interval; falling back to an arbitrary phase causes frame-to-frame
     waveform overlap. */
  if ((trigger_found == 0U) && (first_trigger_found != 0U))
  {
    trigger = first_trigger;
    trigger_found = 1U;
    timebase_limited = 1U;
  }

  if (trigger_found != 0U)
  {
    uint32_t pretrigger = view_samples / 8U;
    trace_start = (trigger > pretrigger) ? (uint32_t)trigger - pretrigger : 0U;
    if (timebase_limited != 0U)
      view_samples = BSP_ADC_CAPTURE_SAMPLE_COUNT - trace_start;
  }
  else
  {
    trace_start = BSP_ADC_CAPTURE_SAMPLE_COUNT - view_samples;
  }

  if (trace_start + view_samples > BSP_ADC_CAPTURE_SAMPLE_COUNT)
    trace_start = BSP_ADC_CAPTURE_SAMPLE_COUNT - view_samples;

  for (uint16_t i = 0U; i < BSP_ADC_SCOPE_POINTS; i++)
  {
    uint16_t source_index = (uint16_t)(trace_start +
        ((uint32_t)i * (view_samples - 1U)) / (BSP_ADC_SCOPE_POINTS - 1U));
    trace[i] = bsp_adc_ring_sample(start_index, source_index);
  }

  /* The acquisition remains raw for measurement/classification.  Square and
     sawtooth traces must also remain raw for drawing: any cross-frame or
     local smoothing smears their true sharp edges.  Sine/triangle retain a
     same-frame median filter to reject isolated ADC spikes. */
  if ((frame->waveform == BSP_ADC_WAVE_SQUARE) ||
      (frame->waveform == BSP_ADC_WAVE_SAWTOOTH))
  {
    for (uint16_t i = 0U; i < BSP_ADC_SCOPE_POINTS; i++)
      frame->samples[i] = trace[i];
  }
  else
  {
    frame->samples[0] = trace[0];
    for (uint16_t i = 1U; i < BSP_ADC_SCOPE_POINTS - 1U; i++)
    {
      uint16_t a = trace[i - 1U];
      uint16_t b = trace[i];
      uint16_t c = trace[i + 1U];
      uint16_t swap;

      if (a > b) { swap = a; a = b; b = swap; }
      if (b > c) { swap = b; b = c; c = swap; }
      if (a > b) { swap = a; a = b; b = swap; }
      frame->samples[i] = b;
    }
    frame->samples[BSP_ADC_SCOPE_POINTS - 1U] = trace[BSP_ADC_SCOPE_POINTS - 1U];
  }
}

uint16_t BSP_ADC_Average(void)
{
  uint32_t sum = 0U;

  if (bsp_adc_start_if_needed() == 0U) return 0U;
  for (uint32_t i = 0U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
    sum += adc_samples[i];

  return (uint16_t)(sum / BSP_ADC_CAPTURE_SAMPLE_COUNT);
}

uint8_t BSP_ADC_GetScopeFrame(BspAdcScopeFrame *frame)
{
  uint32_t sample_rate;
  uint32_t required_ms;
  uint32_t dma_remaining;
  uint16_t start_index;
  uint16_t minimum = 4095U;
  uint16_t maximum = 0U;
  uint16_t threshold;
  uint16_t low_band;
  uint16_t high_band;
  uint16_t first_rising = 0U;
  uint16_t last_rising = 0U;
  uint16_t rising_count = 0U;
  uint8_t rising_armed = 0U;
  uint8_t middle_crossed = 0U;
  uint16_t middle_crossing = 0U;
  uint32_t extrema_count = 0U;
  uint32_t period_samples = 0U;
  uint32_t shape_score_sum = 0U;
  uint32_t shape_score_count = 0U;
  uint16_t large_jump_threshold;
  uint32_t large_jump_count = 0U;

  if (frame == NULL) return 0U;
  memset(frame, 0, sizeof(*frame));
  frame->waveform = BSP_ADC_WAVE_UNKNOWN;

  if (bsp_adc_start_if_needed() == 0U) return 0U;

  sample_rate = bsp_adc_sample_rate_hz();
  if (sample_rate == 0U) return 0U;
  required_ms = (BSP_ADC_CAPTURE_SAMPLE_COUNT * 1000U + sample_rate - 1U) /
                sample_rate;
  if ((uint32_t)(HAL_GetTick() - adc_start_tick) < required_ms) return 0U;

  /* DMA writes the next element at write_index.  That element is the oldest
     item in a complete circular buffer, so it starts a chronological frame. */
  dma_remaining = __HAL_DMA_GET_COUNTER(hadc1.DMA_Handle);
  if (dma_remaining > BSP_ADC_CAPTURE_SAMPLE_COUNT)
    dma_remaining = BSP_ADC_CAPTURE_SAMPLE_COUNT;
  start_index = (uint16_t)((BSP_ADC_CAPTURE_SAMPLE_COUNT - dma_remaining) &
                           BSP_ADC_CAPTURE_MASK);

  for (uint16_t i = 0U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
  {
    uint16_t sample = bsp_adc_ring_sample(start_index, i);
    if (sample < minimum) minimum = sample;
    if (sample > maximum) maximum = sample;
  }

  frame->minimum = minimum;
  frame->maximum = maximum;
  frame->vpp_mv = (uint16_t)(((uint32_t)(maximum - minimum) * 3300U +
                              2047U) / 4095U);

  frame->valid = 1U;
  if ((maximum - minimum) < BSP_ADC_MIN_SPAN_CODES)
  {
    bsp_adc_fill_scope_trace(frame, start_index, sample_rate);
    return 1U;
  }

  threshold = (uint16_t)((minimum + maximum) / 2U);
  low_band = (uint16_t)(minimum + (maximum - minimum) / 8U);
  high_band = (uint16_t)(maximum - (maximum - minimum) / 8U);
  large_jump_threshold = (uint16_t)(((uint32_t)(maximum - minimum) * 2U) / 5U);

  for (uint16_t i = 1U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
  {
    uint16_t previous = bsp_adc_ring_sample(start_index, (uint16_t)(i - 1U));
    uint16_t current = bsp_adc_ring_sample(start_index, i);
    uint16_t step = (current >= previous) ? (current - previous) :
                                            (previous - current);

    if ((current <= low_band) || (current >= high_band)) extrema_count++;
    if (step > large_jump_threshold) large_jump_count++;

    /* Count an edge only after it travelled from the lower to the upper
       hysteresis band.  A noisy sine crossing then cannot create several
       false periods. */
    if (current <= low_band)
    {
      rising_armed = 1U;
      middle_crossed = 0U;
    }
    if ((rising_armed != 0U) && (middle_crossed == 0U) &&
        (previous < threshold) && (current >= threshold))
    {
      middle_crossing = i;
      middle_crossed = 1U;
    }
    if ((rising_armed != 0U) && (current >= high_band))
    {
      if (middle_crossed != 0U)
      {
        if (rising_count == 0U) first_rising = middle_crossing;
        last_rising = middle_crossing;
        rising_count++;
      }
      rising_armed = 0U;
      middle_crossed = 0U;
    }

  }

  if ((rising_count >= 2U) && (last_rising > first_rising))
  {
    uint32_t interval = (uint32_t)last_rising - first_rising;
    uint32_t raw_frequency_x10;
    period_samples = (interval + (rising_count - 1U) / 2U) /
                     (rising_count - 1U);
    raw_frequency_x10 = (sample_rate * 10U * (rising_count - 1U) +
                         interval / 2U) / interval;

    uint32_t target_rate_hz = sample_rate;

    if (sample_rate >= BSP_ADC_FAST_SAMPLE_RATE_HZ)
    {
      if (raw_frequency_x10 <= BSP_ADC_MID_ENTER_HZ_X10)
        target_rate_hz = BSP_ADC_MID_SAMPLE_RATE_HZ;
    }
    else if (sample_rate >= BSP_ADC_MID_SAMPLE_RATE_HZ)
    {
      if (raw_frequency_x10 <= BSP_ADC_LOW_ENTER_HZ_X10)
        target_rate_hz = BSP_ADC_LOW_SAMPLE_RATE_HZ;
      else if (raw_frequency_x10 >= BSP_ADC_FAST_RETURN_HZ_X10)
        target_rate_hz = BSP_ADC_FAST_SAMPLE_RATE_HZ;
    }
    else if (raw_frequency_x10 >= BSP_ADC_MID_RETURN_HZ_X10)
    {
      target_rate_hz = BSP_ADC_MID_SAMPLE_RATE_HZ;
    }

    if (target_rate_hz != sample_rate)
    {
      bsp_adc_set_sample_rate(target_rate_hz);
      return 0U;
    }

    frame->frequency_x10 = bsp_adc_filter_frequency(raw_frequency_x10);
  }

  if (rising_count < 2U)
  {
    if (sample_rate >= BSP_ADC_FAST_SAMPLE_RATE_HZ)
    {
      bsp_adc_set_sample_rate(BSP_ADC_MID_SAMPLE_RATE_HZ);
      return 0U;
    }
    if (sample_rate >= BSP_ADC_MID_SAMPLE_RATE_HZ)
    {
      bsp_adc_set_sample_rate(BSP_ADC_LOW_SAMPLE_RATE_HZ);
      return 0U;
    }

    /* At the lowest rate, a suddenly applied high-frequency signal can
       alias into an invalid trace.  Probe the middle rate again so the
       normal high-frequency path can recover to 80 kHz. */
    bsp_adc_set_sample_rate(BSP_ADC_MID_SAMPLE_RATE_HZ);
    return 0U;
  }

  if (extrema_count * 100U >= BSP_ADC_CAPTURE_SAMPLE_COUNT * 80U)
  {
    uint8_t high_state = (bsp_adc_ring_sample(start_index, 0U) >= threshold) ? 1U : 0U;
    uint32_t run_length = 1U;
    uint32_t longest_high = 0U;
    uint32_t longest_low = 0U;

    /* Measure complete plateaus rather than relying on one selected edge.
       The first/last DMA pieces are naturally shorter; the longest high and
       low runs come from a complete square-wave cycle.  Hysteresis keeps
       ADC noise around the midpoint from splitting a plateau. */
    for (uint16_t i = 1U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
    {
      uint16_t sample = bsp_adc_ring_sample(start_index, i);

      if ((high_state != 0U) && (sample <= low_band))
      {
        if (run_length > longest_high) longest_high = run_length;
        high_state = 0U;
        run_length = 1U;
      }
      else if ((high_state == 0U) && (sample >= high_band))
      {
        if (run_length > longest_low) longest_low = run_length;
        high_state = 1U;
        run_length = 1U;
      }
      else
      {
        run_length++;
      }
    }

    if (high_state != 0U)
    {
      if (run_length > longest_high) longest_high = run_length;
    }
    else
    {
      if (run_length > longest_low) longest_low = run_length;
    }

    frame->waveform = BSP_ADC_WAVE_SQUARE;
    frame->duty_x10 = ((longest_high + longest_low) != 0U) ?
                      (uint16_t)((longest_high * 1000U +
                                  (longest_high + longest_low) / 2U) /
                                  (longest_high + longest_low)) :
                      0U;
    bsp_adc_fill_scope_trace(frame, start_index, sample_rate);
    return 1U;
  }

  /* A sawtooth has one large return edge in nearly every period.  Requiring
     repeated full-span jumps avoids classifying a triangle from one noisy
     ADC sample or one DMA-boundary artifact. */
  if ((large_jump_count != 0U) && (large_jump_count * 2U >= rising_count))
  {
    frame->waveform = BSP_ADC_WAVE_SAWTOOTH;
    bsp_adc_fill_scope_trace(frame, start_index, sample_rate);
    return 1U;
  }

  /* Compare the waveform at four phase points after a confirmed rising
     midpoint crossing.  Triangle: 75% of span at +/-T/8; sine: about 85%.
     This is much less sensitive to ADC staircase sampling than derivative
     variance, and remains independent of amplitude and DC offset. */
  rising_armed = 0U;
  middle_crossed = 0U;
  for (uint16_t i = 1U; i < BSP_ADC_CAPTURE_SAMPLE_COUNT; i++)
  {
    uint16_t previous = bsp_adc_ring_sample(start_index, (uint16_t)(i - 1U));
    uint16_t current = bsp_adc_ring_sample(start_index, i);

    if (current <= low_band)
    {
      rising_armed = 1U;
      middle_crossed = 0U;
    }
    if ((rising_armed != 0U) && (middle_crossed == 0U) &&
        (previous < threshold) && (current >= threshold))
    {
      middle_crossing = i;
      middle_crossed = 1U;
    }
    if ((rising_armed != 0U) && (current >= high_band))
    {
      if ((middle_crossed != 0U) &&
          ((uint32_t)middle_crossing + period_samples * 7U / 8U <
           BSP_ADC_CAPTURE_SAMPLE_COUNT))
      {
        const uint8_t phases[4] = { 1U, 3U, 5U, 7U };
        for (uint8_t phase = 0U; phase < 4U; phase++)
        {
          uint16_t sample = bsp_adc_ring_sample(start_index,
              (uint16_t)((uint32_t)middle_crossing +
              period_samples * phases[phase] / 8U));
          uint32_t level = ((uint32_t)(sample - minimum) * 1000U +
                            (maximum - minimum) / 2U) /
                           (maximum - minimum);
          shape_score_sum += (phase < 2U) ? level : (1000U - level);
          shape_score_count++;
        }
      }
      rising_armed = 0U;
      middle_crossed = 0U;
    }
  }

  /* Every periodic non-square/non-saw signal starts as sine. */
  frame->waveform = BSP_ADC_WAVE_SINE;
  if ((shape_score_count >= 4U) &&
      (shape_score_sum < shape_score_count * 800U))
    frame->waveform = BSP_ADC_WAVE_TRIANGLE;

  bsp_adc_fill_scope_trace(frame, start_index, sample_rate);
  return 1U;
}

void BSP_DAC_StartTestWave(void)
{
  BspDacConfig config = {
    BSP_DAC_WAVE_SQUARE, 16U, 50U, 1000U, 1U
  };
  (void)BSP_DAC_ApplyConfig(&config);
}

uint8_t BSP_DAC_ApplyConfig(const BspDacConfig *config)
{
  uint32_t vpp_code;
  uint32_t low_code;
  uint32_t high_code;
  BspDacConfig safe_config;

  if (config == NULL) return 0U;

  safe_config = *config;
  if (safe_config.waveform > BSP_DAC_WAVE_SAWTOOTH)
    safe_config.waveform = BSP_DAC_WAVE_SINE;
  if (safe_config.vpp_x10 > BSP_DAC_VREF_X10)
    safe_config.vpp_x10 = BSP_DAC_VREF_X10;
  if (safe_config.duty_percent > 100U)
    safe_config.duty_percent = 100U;
  if (safe_config.frequency_x10 > 20000U)
    safe_config.frequency_x10 = 20000U;

  /* Stop before replacing the buffer.  This prevents DMA from reading a
     half-updated waveform table. */
  (void)HAL_TIM_Base_Stop(&htim6);
  (void)HAL_DAC_Stop_DMA(&hdac, DAC_CHANNEL_1);

  vpp_code = ((uint32_t)safe_config.vpp_x10 * 4095U) / BSP_DAC_VREF_X10;
  low_code = (BSP_DAC_MID_CODE > (vpp_code / 2U)) ?
             BSP_DAC_MID_CODE - (vpp_code / 2U) : 0U;
  high_code = BSP_DAC_MID_CODE + ((vpp_code + 1U) / 2U);
  if (high_code > 4095U) high_code = 4095U;

  for (uint32_t i = 0U; i < BSP_DAC_SAMPLE_COUNT; i++)
  {
    dac_samples[i] = bsp_dac_wave_sample(&safe_config, i, low_code, high_code);
  }

  if ((safe_config.running != 0U) && (safe_config.frequency_x10 != 0U))
  {
    bsp_dac_configure_timer(safe_config.frequency_x10);
    if (HAL_DAC_Start_DMA(&hdac, DAC_CHANNEL_1, (uint32_t *)dac_samples,
                          BSP_DAC_SAMPLE_COUNT, DAC_ALIGN_12B_R) != HAL_OK)
      return 0U;
    if (HAL_TIM_Base_Start(&htim6) != HAL_OK)
    {
      (void)HAL_DAC_Stop_DMA(&hdac, DAC_CHANNEL_1);
      return 0U;
    }
  }
  else
  {
    /* At 0 Hz or while paused, hold the DAC at the midpoint. */
    if (HAL_DAC_SetValue(&hdac, DAC_CHANNEL_1, DAC_ALIGN_12B_R,
                         BSP_DAC_MID_CODE) != HAL_OK)
      return 0U;
  }

  return 1U;
}

void BSP_DAC_Stop(void)
{
  (void)HAL_TIM_Base_Stop(&htim6);
  (void)HAL_DAC_Stop_DMA(&hdac, DAC_CHANNEL_1);
  (void)HAL_DAC_SetValue(&hdac, DAC_CHANNEL_1, DAC_ALIGN_12B_R,
                         BSP_DAC_MID_CODE);
}

uint8_t BSP_Audio_InitAndStartTone(void)
{
  if (WM8978_Init() != 0U) return 0U;
  WM8978_ADDA_Cfg(1U, 0U);
  WM8978_Input_Cfg(0U, 0U, 0U);
  WM8978_Output_Cfg(1U, 0U);
  WM8978_I2S_Cfg(2U, 0U);
  WM8978_HPvol_Set(45U, 45U);
  WM8978_SPKvol_Set(42U);
  for (uint32_t frame = 0U; frame < 64U; frame++) {
    uint16_t level = ((frame / 4U) & 1U) ? 0x1800U : 0xE800U;
    audio_samples[frame * 2U] = level;
    audio_samples[frame * 2U + 1U] = level;
  }
  if (HAL_I2S_Transmit_DMA(&hi2s2, audio_samples, 128U) != HAL_OK) return 0U;
  audio_started = 1U;
  return 1U;
}

void BSP_Audio_Stop(void)
{
  if (audio_started != 0U) (void)HAL_I2S_DMAStop(&hi2s2);
  audio_started = 0U;
}

void BSP_Board_Init(void)
{
  delay_init();
  BSP_LED_Set(0U, 0U);
  BSP_LED_Set(1U, 0U);
  BSP_Buzzer_Set(0U);
  BSP_Alarm_Init();
  BSP_Touch_Init();
//  BSP_DAC_StartTestWave();
  (void)HAL_TIM_Base_Start(&htim3);
  (void)BSP_ADC_Average();
}

uint8_t BSP_LCD_InitAndShow(void)
{
  LCD_Init();
  LCD_Clear(BLACK);
  POINT_COLOR = GREEN;
  LCD_ShowString(12U, 12U, lcddev.width, lcddev.height, 16U, (u8 *)"SmartClock F407");
  POINT_COLOR = GREEN;
  LCD_ShowString(12U, 34U, lcddev.width, lcddev.height, 16U, (u8 *)"LCD driver: OK");
  return (lcddev.id != 0U);
}

uint8_t BSP_SD_MountAndProbe(void)
{
  DIR directory;
  FRESULT result = f_mount(&USERFatFS, USERPath, 1U);
  if (result != FR_OK) return (uint8_t)result;
  result = f_opendir(&directory, USERPath);
  if (result == FR_OK) f_closedir(&directory);
  return (uint8_t)result;
}
