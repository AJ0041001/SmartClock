#include "../Inc/bsp_board.h"
#include "main.h"
#include "delay.h"
#include "bsp_touch.h"
#include "wm8978.h"
#include "lcd.h"
#include "fatfs.h"
#include "cmsis_os.h"
#include "FreeRTOS.h"
#include "task.h"
#include <stdio.h>
#include <string.h>

extern ADC_HandleTypeDef hadc1;
extern DAC_HandleTypeDef hdac;
extern I2S_HandleTypeDef hi2s2;
extern TIM_HandleTypeDef htim3;
extern TIM_HandleTypeDef htim6;
extern UART_HandleTypeDef huart1;
extern UART_HandleTypeDef huart2;
extern RTC_HandleTypeDef hrtc;

#define BSP_ADC_CAPTURE_SAMPLE_COUNT 4096U
#define BSP_ADC_CAPTURE_MASK         (BSP_ADC_CAPTURE_SAMPLE_COUNT - 1U)
#define BSP_ADC_MIN_SPAN_CODES       40U
#define BSP_ADC_VALID_MARGIN         (BSP_ADC_CAPTURE_SAMPLE_COUNT / 32U)
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
/* Read mono PCM, then expand it in-place to L/R for I2S2. */
#define BSP_AUDIO_PCM_FRAMES 2048U
static uint16_t audio_wav_stereo[BSP_AUDIO_PCM_FRAMES * 2U];
static uint8_t adc_started;
static uint8_t audio_started;
static uint8_t audio_codec_ready;
static uint8_t audio_fs_mounted;
static volatile uint8_t audio_last_fresult;
static volatile uint8_t audio_last_status;
static volatile uint8_t audio_last_hour;
static volatile uint8_t audio_dma_done;
static volatile uint8_t audio_dma_error;
static volatile uint8_t audio_output_muted = 1U;
/* The SD card supplied for this project contains the Taffy files t1..t24.
   Keep Taffy as the power-on default; the UI can still select the default
   1..24.wav pack explicitly. */
static volatile uint8_t audio_voice_pack = 1U;
/* 0 = MO LE files in the SD-card root, 1 = Miao files in /Miao. */
static volatile uint8_t audio_game_music_pack;
static volatile uint8_t audio_volume_percent = 70U;
#define BSP_AUDIO_EFFECT_QUEUE_SIZE 8U
static volatile uint8_t audio_effect_queue[BSP_AUDIO_EFFECT_QUEUE_SIZE];
static volatile uint8_t audio_effect_head;
static volatile uint8_t audio_effect_tail;
static volatile uint8_t audio_effect_playing;
static volatile uint8_t audio_effect_cancel;
static volatile uint8_t audio_playback_active;
static volatile uint8_t audio_hour_pending;
static volatile uint8_t audio_hour_value;
static uint8_t audio_effect_retry_count;
/* Enable hourly voice chime after reset; the Voice Packs page can still turn
   it off explicitly.  This setting is runtime-only and is not persisted. */
static volatile uint8_t bsp_time_chime_enabled;
static volatile uint8_t bsp_time_chime_time_editing;
static volatile uint8_t bsp_time_chime_after_edit_pending;
/* USART2 game-control receiver.  USART2_RX is mapped to DMA1 Stream5 on
   STM32F407, but Stream5 is already used by DAC channel 1.  Use receive-to-
   idle interrupt mode here so the DAC waveform DMA remains untouched. */
#define BSP_GAME_UART_DMA_SIZE 64U
#define BSP_GAME_FRAME_SIZE    10U
#define BSP_GAME_RESPONSE_QUEUE_SIZE 32U
static uint8_t bsp_game_uart_dma[BSP_GAME_UART_DMA_SIZE];
static uint8_t bsp_game_frame[BSP_GAME_FRAME_SIZE];
static uint8_t bsp_game_frame_index;
static uint16_t bsp_game_uart_last_pos;
static volatile uint8_t bsp_game_response_queue[BSP_GAME_RESPONSE_QUEUE_SIZE];
static volatile uint8_t bsp_game_response_head;
static volatile uint8_t bsp_game_response_tail;
static volatile uint8_t bsp_game_serial_mode = 2U;
static volatile uint16_t bsp_game_serial_x;
static volatile uint16_t bsp_game_serial_y;
static volatile uint32_t bsp_game_serial_generation;
static volatile uint32_t bsp_game_serial_last_tick;
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

static uint8_t bsp_game_crc8(const uint8_t *data, uint8_t length)
{
  uint8_t crc = 0U;

  for (uint8_t i = 0U; i < length; i++)
  {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; bit++)
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1U) ^ 0x07U) : (uint8_t)(crc << 1U);
  }
  return crc;
}

static void bsp_game_queue_response(uint8_t ok)
{
  uint8_t next = (uint8_t)((bsp_game_response_head + 1U) %
                           BSP_GAME_RESPONSE_QUEUE_SIZE);

  /* This function is called from the DMA/USART interrupt context.  Queue only
     the result here; the actual UART transmission is done by the LVGL task. */
  if (next == bsp_game_response_tail) return;
  bsp_game_response_queue[bsp_game_response_head] = (ok != 0U) ? 1U : 0U;
  bsp_game_response_head = next;
}

static void bsp_game_publish_frame(void)
{
  uint8_t type = bsp_game_frame[3];
  uint8_t valid = 1U;
  uint16_t x = (uint16_t)bsp_game_frame[4] |
               (uint16_t)((uint16_t)bsp_game_frame[5] << 8U);
  uint16_t y = (uint16_t)bsp_game_frame[6] |
               (uint16_t)((uint16_t)bsp_game_frame[7] << 8U);

  if (bsp_game_frame[0] != 0xAAU || bsp_game_frame[1] != 0x55U) valid = 0U;
  if (bsp_game_frame[2] != 6U) valid = 0U;
  if ((type != 1U) && (type != 2U)) valid = 0U;
  if (bsp_game_crc8(&bsp_game_frame[2], 7U) != bsp_game_frame[9]) valid = 0U;

  bsp_game_queue_response(valid);
  if (valid == 0U) return;

  /* The 32-bit generation lets the LVGL task consume only new frames.  The
     interrupt never touches an LVGL object. */
  bsp_game_serial_mode = type;
  bsp_game_serial_x = x;
  bsp_game_serial_y = y;
  bsp_game_serial_last_tick = HAL_GetTick();
  bsp_game_serial_generation++;
}

static void bsp_game_feed_byte(uint8_t byte)
{
  if (bsp_game_frame_index == 0U)
  {
    if (byte == 0xAAU) bsp_game_frame[bsp_game_frame_index++] = byte;
    return;
  }

  if (bsp_game_frame_index == 1U)
  {
    if (byte == 0x55U)
    {
      bsp_game_frame[bsp_game_frame_index++] = byte;
    }
    else
    {
      bsp_game_frame_index = (byte == 0xAAU) ? 1U : 0U;
      if (bsp_game_frame_index != 0U) bsp_game_frame[0] = byte;
    }
    return;
  }

  bsp_game_frame[bsp_game_frame_index++] = byte;
  if (bsp_game_frame_index >= BSP_GAME_FRAME_SIZE)
  {
    bsp_game_publish_frame();
    bsp_game_frame_index = 0U;
  }
}

void BSP_GameSerial_Start(void)
{
  bsp_game_frame_index = 0U;
  bsp_game_uart_last_pos = 0U;
  bsp_game_serial_mode = 2U;
  bsp_game_serial_x = 0U;
  bsp_game_serial_y = 0U;
  bsp_game_serial_generation = 0U;
  bsp_game_serial_last_tick = 0U;
  bsp_game_response_head = 0U;
  bsp_game_response_tail = 0U;
  (void)HAL_UARTEx_ReceiveToIdle_IT(&huart2, bsp_game_uart_dma,
                                    BSP_GAME_UART_DMA_SIZE);
}

void BSP_GameSerial_Service(void)
{
  static const uint8_t ok_response[] = {'O', 'K', '\r', '\n'};
  static const uint8_t error_response[] = {'E', 'R', 'R', 'O', 'R', '\r', '\n'};
  uint8_t result;
  uint8_t count = 0U;

  /* Keep the per-call work bounded so a burst of test frames cannot make the
     LVGL task stay in UART transmission for an unbounded time. */
  while ((bsp_game_response_tail != bsp_game_response_head) && (count < 4U))
  {
    result = bsp_game_response_queue[bsp_game_response_tail];
    bsp_game_response_tail = (uint8_t)((bsp_game_response_tail + 1U) %
                                       BSP_GAME_RESPONSE_QUEUE_SIZE);
    if (result != 0U)
      (void)HAL_UART_Transmit(&huart2, (uint8_t *)ok_response,
                              (uint16_t)sizeof(ok_response), 10U);
    else
      (void)HAL_UART_Transmit(&huart2, (uint8_t *)error_response,
                              (uint16_t)sizeof(error_response), 10U);
    count++;
  }
}

uint8_t BSP_GameSerial_GetLatest(BspGameSerialControl *control)
{
  uint32_t generation_a;
  uint32_t generation_b;

  if (control == NULL) return 0U;

  /* Retry if the DMA callback updates the snapshot while it is being read. */
  do
  {
    generation_a = bsp_game_serial_generation;
    control->mode = bsp_game_serial_mode;
    control->x = bsp_game_serial_x;
    control->y = bsp_game_serial_y;
    control->last_tick = bsp_game_serial_last_tick;
    generation_b = bsp_game_serial_generation;
  } while (generation_a != generation_b);

  control->generation = generation_b;
  return (generation_b != 0U) ? 1U : 0U;
}

void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *huart, uint16_t size)
{
  uint16_t i;

  if (huart != &huart2) return;

  /* Interrupt-mode reception uses a linear buffer.  Size is the number of
     valid bytes in this callback, not a circular DMA write position. */
  if (size > BSP_GAME_UART_DMA_SIZE) size = BSP_GAME_UART_DMA_SIZE;
  for (i = 0U; i < size; i++) bsp_game_feed_byte(bsp_game_uart_dma[i]);
  bsp_game_uart_last_pos = 0U;

  /* Re-arm after both IDLE and full-buffer events. */
  (void)HAL_UARTEx_ReceiveToIdle_IT(&huart2, bsp_game_uart_dma,
                                    BSP_GAME_UART_DMA_SIZE);
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if (huart == &huart2)
  {
    bsp_game_uart_last_pos = 0U;
    bsp_game_frame_index = 0U;
    (void)HAL_UARTEx_ReceiveToIdle_IT(&huart2, bsp_game_uart_dma,
                                      BSP_GAME_UART_DMA_SIZE);
  }
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

/* The raw period estimate is quantized to ADC sample positions.  A short
   history makes the reported value stable while keeping a genuine, large
   frequency change responsive by resetting the history. */
static uint32_t bsp_adc_filter_frequency(uint32_t raw_frequency_x10)
{
  uint32_t average;
  uint32_t difference;
  uint32_t sorted[4];
  uint32_t swap;

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

  if (adc_frequency_count < 4U)
    return (adc_frequency_sum + adc_frequency_count / 2U) /
           adc_frequency_count;

  /* With four samples, use the middle two instead of the arithmetic mean.
     One incorrectly detected edge can then affect only one frame and cannot
     pull the displayed frequency toward a wrong value. */
  for (uint8_t i = 0U; i < 4U; i++)
    sorted[i] = adc_frequency_history[i];
  for (uint8_t i = 0U; i < 3U; i++)
  {
    for (uint8_t j = (uint8_t)(i + 1U); j < 4U; j++)
    {
      if (sorted[j] < sorted[i])
      {
        swap = sorted[i];
        sorted[i] = sorted[j];
        sorted[j] = swap;
      }
    }
  }
  return (sorted[1] + sorted[2] + 1U) / 2U;
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

  /* Keep acquisition/measurement raw, but remove isolated display spikes
     with a 3-point median.  A median does not smear a square-wave edge like
     an arithmetic average would, and also makes sawtooth lines steadier. */
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
  uint32_t first_rising_x10 = 0U;
  uint32_t last_rising_x10 = 0U;
  uint16_t rising_count = 0U;
  uint8_t rising_armed = 0U;
  uint8_t middle_crossed = 0U;
  uint16_t middle_crossing = 0U;
  uint32_t middle_crossing_x10 = 0U;
  uint32_t extrema_count = 0U;
  uint32_t period_samples = 0U;
  uint32_t shape_score_sum = 0U;
  uint32_t shape_score_count = 0U;
  uint16_t large_jump_threshold;
  uint32_t large_jump_count = 0U;
  uint64_t adc_sum = 0U;

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
    adc_sum += sample;
    if (sample < minimum) minimum = sample;
    if (sample > maximum) maximum = sample;
  }

  frame->minimum = minimum;
  frame->maximum = maximum;
  frame->dc_mv = (uint16_t)((adc_sum * 3300U +
                             (BSP_ADC_CAPTURE_SAMPLE_COUNT * 4095U) / 2U) /
                            (BSP_ADC_CAPTURE_SAMPLE_COUNT * 4095U));
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

  /* Ignore the short, potentially incomplete waveform portions at both DMA
     boundaries.  Counting only the central 92% prevents a boundary crossing
     from being paired with the next frame and distorting the frequency. */
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
      middle_crossing_x10 = (uint32_t)(i - 1U) * 10U;
      if (current > previous)
      {
        /* Estimate the threshold crossing between two ADC samples instead
           of rounding it to one whole sample.  At 400Hz/80kHz one sample is
           already 0.5% of a period, so this noticeably improves accuracy. */
        middle_crossing_x10 += ((uint32_t)(threshold - previous) * 10U) /
                               (uint32_t)(current - previous);
      }
      middle_crossed = 1U;
    }
    if ((rising_armed != 0U) && (current >= high_band))
    {
      if (middle_crossed != 0U)
      {
        if ((middle_crossing >= BSP_ADC_VALID_MARGIN) &&
            (middle_crossing <
             (BSP_ADC_CAPTURE_SAMPLE_COUNT - BSP_ADC_VALID_MARGIN)))
        {
          if (rising_count == 0U)
            first_rising_x10 = middle_crossing_x10;
          last_rising_x10 = middle_crossing_x10;
          rising_count++;
        }
      }
      rising_armed = 0U;
      middle_crossed = 0U;
    }

  }

  if ((rising_count >= 2U) && (last_rising_x10 > first_rising_x10))
  {
    uint32_t interval_x10 = last_rising_x10 - first_rising_x10;
    uint32_t raw_frequency_x10;
    uint64_t cycle_count = (uint64_t)(rising_count - 1U);
    uint64_t interval_product = (uint64_t)sample_rate * 100U * cycle_count;

    period_samples = (interval_x10 + 5U) / 10U /
                     (rising_count - 1U);
    raw_frequency_x10 = (uint32_t)((interval_product + interval_x10 / 2U) /
                                   interval_x10);

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
  /* Normal-mode DMA already returns the HAL handle to READY.  Only abort a
     transfer that is genuinely still busy; aborting an already completed
     transfer can leave the codec/I2S clock path silent for the next effect. */
  if (HAL_I2S_GetState(&hi2s2) == HAL_I2S_STATE_BUSY_TX)
    (void)HAL_I2S_DMAStop(&hi2s2);
  audio_started = 0U;
}

static void bsp_audio_recover_after_failure(void)
{
  /* Do not leave a failed DMA/I2S state latched.  The next request will
     prepare the codec again and can therefore recover after an interrupted
     transfer, SD read error, or a transient I2S fault. */
  if (HAL_I2S_GetState(&hi2s2) != HAL_I2S_STATE_READY)
    (void)HAL_I2S_DMAStop(&hi2s2);
  audio_started = 0U;
  audio_dma_done = 0U;
  audio_dma_error = 0U;
  audio_output_muted = 1U;
  audio_codec_ready = 0U;
}

static uint16_t bsp_audio_le16(const uint8_t *data)
{
  return (uint16_t)data[0] | ((uint16_t)data[1] << 8U);
}

static uint32_t bsp_audio_le32(const uint8_t *data)
{
  return (uint32_t)data[0] | ((uint32_t)data[1] << 8U) |
         ((uint32_t)data[2] << 16U) | ((uint32_t)data[3] << 24U);
}

static uint8_t bsp_audio_codec_prepare(void)
{
  if (audio_codec_ready != 0U) return 1U;
  if (WM8978_Init() != 0U) return 0U;

  WM8978_ADDA_Cfg(1U, 0U);
  WM8978_Input_Cfg(0U, 0U, 0U);
  WM8978_Output_Cfg(1U, 0U);
  WM8978_I2S_Cfg(2U, 0U);       /* Philips I2S, 16-bit samples. */
  WM8978_Write_Reg(7U, 3U << 1U); /* SR=011: codec digital filters at 16 kHz. */
  /* Keep analogue output muted while the codec power and clocks settle. */
  WM8978_HPvol_Set(0U, 0U);
  WM8978_SPKvol_Set(0U);
  osDelay(5U);
  audio_output_muted = 1U;
  audio_codec_ready = 1U;
  return 1U;
}

static uint8_t bsp_audio_volume_code(void)
{
  uint32_t code = ((uint32_t)audio_volume_percent * 63U + 50U) / 100U;
  if (audio_volume_percent != 0U && code == 0U) code = 1U;
  return (uint8_t)code;
}

static void bsp_audio_apply_volume(void)
{
  uint8_t code = bsp_audio_volume_code();
  WM8978_HPvol_Set(code, code);
  WM8978_SPKvol_Set(code);
}

void HAL_I2S_TxCpltCallback(I2S_HandleTypeDef *hi2s)
{
  if (hi2s == &hi2s2) audio_dma_done = 1U;
}

void HAL_I2S_ErrorCallback(I2S_HandleTypeDef *hi2s)
{
  if (hi2s == &hi2s2) audio_dma_error = 1U;
}

static uint8_t bsp_audio_transmit_dma(uint16_t *samples, uint16_t sample_count)
{
  uint32_t start_tick;

  audio_dma_done = 0U;
  audio_dma_error = 0U;
  if (HAL_I2S_Transmit_DMA(&hi2s2, samples, sample_count) != HAL_OK)
  {
    /* A previous short effect may have left the I2S handle busy.  Reset the
       DMA state once and retry so every collision can start a new effect. */
    if (HAL_I2S_GetState(&hi2s2) == HAL_I2S_STATE_BUSY_TX)
      (void)HAL_I2S_DMAStop(&hi2s2);
    osDelay(1U);
    if (HAL_I2S_Transmit_DMA(&hi2s2, samples, sample_count) != HAL_OK)
      return 0U;
  }
  audio_started = 1U;

  /* Let the first few PCM samples reach the codec muted, then open the
     analogue output.  This avoids the WM8978 power-up POP and preserves the
     beginning of the voice file. */
  if (audio_output_muted != 0U)
  {
    osDelay(5U);
    bsp_audio_apply_volume();
    audio_output_muted = 0U;
  }

  start_tick = HAL_GetTick();
  while (audio_dma_done == 0U && audio_dma_error == 0U)
  {
    /* A newer game event may preempt the current effect.  With no newer
       event this flag stays clear and the current WAV chunk plays fully. */
    if (audio_playback_active != 0U && audio_effect_cancel != 0U)
    {
      if (HAL_I2S_GetState(&hi2s2) == HAL_I2S_STATE_BUSY_TX)
        (void)HAL_I2S_DMAStop(&hi2s2);
      audio_started = 0U;
      return 0U;
    }
    if ((uint32_t)(HAL_GetTick() - start_tick) > 1000U)
    {
      (void)HAL_I2S_DMAStop(&hi2s2);
      audio_started = 0U;
      return 0U;
    }
    /* The DMA continues in hardware; let LVGL and other RTOS tasks run. */
    osDelay(1U);
  }

  return (audio_dma_error == 0U) ? 1U : 0U;
}

static uint8_t bsp_audio_wait_i2s_dma(void)
{
  uint32_t start = HAL_GetTick();

  while (HAL_I2S_GetState(&hi2s2) == HAL_I2S_STATE_BUSY_TX)
  {
    if ((uint32_t)(HAL_GetTick() - start) > 500U) return 0U;
    osDelay(1U);
  }
  return (HAL_I2S_GetState(&hi2s2) == HAL_I2S_STATE_READY) ? 1U : 0U;
}

static uint8_t bsp_audio_wav_find_data(FIL *file, FSIZE_t *data_offset,
                                       FSIZE_t *data_size)
{
  uint8_t riff_header[12];
  uint8_t chunk_header[8];
  uint8_t fmt_header[16];
  UINT bytes_read;
  FSIZE_t position = 12U;
  FSIZE_t file_size;
  uint8_t fmt_ok = 0U;

  if (f_lseek(file, 0U) != FR_OK) return 0U;
  if (f_read(file, riff_header, sizeof(riff_header), &bytes_read) != FR_OK ||
      bytes_read != sizeof(riff_header)) return 0U;
  if (memcmp(riff_header, "RIFF", 4U) != 0 ||
      memcmp(&riff_header[8], "WAVE", 4U) != 0) return 0U;

  file_size = f_size(file);
  while ((position + 8U) <= file_size)
  {
    uint32_t chunk_size;
    if (f_lseek(file, position) != FR_OK) return 0U;
    if (f_read(file, chunk_header, sizeof(chunk_header), &bytes_read) != FR_OK ||
        bytes_read != sizeof(chunk_header)) return 0U;
    chunk_size = bsp_audio_le32(&chunk_header[4]);
    position += 8U;

    if (memcmp(chunk_header, "fmt ", 4U) == 0)
    {
      if (chunk_size < sizeof(fmt_header) ||
          f_read(file, fmt_header, sizeof(fmt_header), &bytes_read) != FR_OK ||
          bytes_read != sizeof(fmt_header)) return 0U;
      /* PCM, mono, 16-bit, 16 kHz: exactly what the converted files use. */
      fmt_ok = (uint8_t)(bsp_audio_le16(&fmt_header[0]) == 1U &&
                         bsp_audio_le16(&fmt_header[2]) == 1U &&
                         bsp_audio_le32(&fmt_header[4]) == 16000U &&
                         bsp_audio_le16(&fmt_header[14]) == 16U);
    }
    else if (memcmp(chunk_header, "data", 4U) == 0)
    {
      *data_offset = position;
      *data_size = chunk_size;
      return (uint8_t)(fmt_ok != 0U && (*data_offset + *data_size) <= file_size);
    }

    position += (FSIZE_t)chunk_size + (chunk_size & 1U);
  }
  return 0U;
}

static uint8_t bsp_audio_play_file(const char *path, uint8_t hour)
{
  FIL file;
  FSIZE_t data_offset;
  FSIZE_t data_size;
  FSIZE_t remaining;
  UINT bytes_read;
  uint8_t success = 0U;
  FRESULT result;

  audio_last_hour = hour;
  audio_last_status = 0U;

  if (audio_fs_mounted == 0U)
  {
    result = f_mount(&USERFatFS, USERPath, 1U);
    audio_last_fresult = (uint8_t)result;
    if (result != FR_OK)
    {
      audio_last_status = 1U; /* mount failed */
      return 0U;
    }
    audio_fs_mounted = 1U;
  }

  result = f_open(&file, path, FA_READ);
  audio_last_fresult = (uint8_t)result;
  if (result != FR_OK)
  {
    audio_last_status = 2U; /* requested hour file not found/open failed */
    return 0U;
  }
  if (bsp_audio_wav_find_data(&file, &data_offset, &data_size) == 0U)
  {
    (void)f_close(&file);
    audio_last_status = 3U; /* file is not the expected 16 kHz PCM WAV */
    return 0U;
  }
  if (f_lseek(&file, data_offset) != FR_OK)
  {
    (void)f_close(&file);
    audio_last_status = 4U; /* data chunk seek failed */
    return 0U;
  }

  /* Do not power up/unmute WM8978 until the SD file has been found and
     validated. Otherwise a missing/invalid file still produces the codec's
     one-time analogue power-up POP during every reset. */
  if (bsp_audio_codec_prepare() == 0U)
  {
    (void)f_close(&file);
    audio_last_status = 5U; /* WM8978/I2S preparation failed */
    return 0U;
  }

  remaining = data_size;
  while (remaining >= 2U)
  {
    UINT requested = (remaining > (FSIZE_t)(BSP_AUDIO_PCM_FRAMES * 2U)) ?
                     (BSP_AUDIO_PCM_FRAMES * 2U) : (UINT)remaining;
    result = f_read(&file, audio_wav_stereo, requested, &bytes_read);
    audio_last_fresult = (uint8_t)result;
    if (result != FR_OK || bytes_read < 2U)
    {
      audio_last_status = 6U; /* PCM data read failed */
      break;
    }

    bytes_read &= ~1U;
    /* Expand backwards so the mono source samples are not overwritten. */
    for (uint32_t i = (bytes_read / 2U); i > 0U; i--)
    {
      uint16_t sample = audio_wav_stereo[i - 1U];
      audio_wav_stereo[(i - 1U) * 2U] = sample;
      audio_wav_stereo[(i - 1U) * 2U + 1U] = sample;
    }
    /* Send the finite PCM chunk with normal-mode DMA.  The task sleeps while
       DMA clocks the samples out, so LVGL is not blocked by audio transfer. */
    if (bsp_audio_transmit_dma(audio_wav_stereo,
                               (uint16_t)((bytes_read / 2U) * 2U)) == 0U)
    {
      audio_last_status = 7U; /* I2S transmit failed */
      break;
    }
    remaining -= bytes_read;
  }

  /* A file is successful only after every PCM byte was transmitted.  This
     matters for recovery: a long file that fails after its first DMA chunk
     must not be reported as a successful playback. */
  if (remaining == 0U)
    success = 1U;

  /* Mute before stopping I2S so the final clock transition is silent. */
  if (audio_output_muted == 0U)
  {
    WM8978_HPvol_Set(0U, 0U);
    WM8978_SPKvol_Set(0U);
    osDelay(10U);
    audio_output_muted = 1U;
  }
  BSP_Audio_Stop();
  (void)f_close(&file);
  if (success == 0U && audio_last_status == 7U)
    bsp_audio_recover_after_failure();
  if (success != 0U) audio_last_status = 8U; /* playback succeeded */
  return success;
}

uint8_t BSP_Audio_PlayHour(uint8_t hour)
{
  char path[20];

  if (hour < 1U || hour > 24U) return 0U;
  if (audio_voice_pack == 0U)
    (void)snprintf(path, sizeof(path), "%s%u.wav", USERPath, hour);
  else
    (void)snprintf(path, sizeof(path), "%st%u.wav", USERPath, hour);
  return bsp_audio_play_file(path, hour);
}

uint8_t BSP_Audio_PlayEffect(BspAudioEffect effect)
{
  const char *name = NULL;
  const char *folder = "";
  char path[40];

  if (audio_game_music_pack != 0U)
  {
    static const char *const miao_star_files[5] =
        {"xxl1.wav", "xxl2.wav", "xxl3.wav", "xxl4.wav", "xxl5.wav"};
    static uint32_t miao_random_state = 0x61C88647UL;

    folder = "/Miao/";
    switch (effect)
    {
      case BSP_AUDIO_EFFECT_STAR1:
      case BSP_AUDIO_EFFECT_STAR2:
        miao_random_state = miao_random_state * 1664525UL +
                            1013904223UL + HAL_GetTick();
        name = miao_star_files[miao_random_state % 5U];
        break;
      case BSP_AUDIO_EFFECT_GAMEOVER: name = "xxljs.wav"; break;
      case BSP_AUDIO_EFFECT_BOMB:     name = "xxlzd.wav"; break;
      default: return 0U;
    }
  }
  else
  {
    switch (effect)
    {
      case BSP_AUDIO_EFFECT_STAR1:    name = "xing1.wav"; break;
      case BSP_AUDIO_EFFECT_STAR2:    name = "xing2.wav"; break;
      case BSP_AUDIO_EFFECT_GAMEOVER: name = "shule16.wav"; break;
      case BSP_AUDIO_EFFECT_BOMB:     name = "bbb16.wav"; break;
      default: return 0U;
    }
  }

  (void)snprintf(path, sizeof(path), "%s%s%s", USERPath, folder, name);
  return bsp_audio_play_file(path, 0U);
}

void BSP_Audio_SetVoicePack(uint8_t pack)
{
  audio_voice_pack = (pack != 0U) ? 1U : 0U;
}

void BSP_Audio_SetGameMusicPack(uint8_t pack)
{
  audio_game_music_pack = (pack != 0U) ? 1U : 0U;
}

void BSP_Audio_SetVolume(uint8_t percent)
{
  if (percent > 100U) percent = 100U;
  audio_volume_percent = percent;
  if (audio_codec_ready != 0U && audio_output_muted == 0U)
    bsp_audio_apply_volume();
}

uint8_t BSP_Audio_GetVolume(void)
{
  return audio_volume_percent;
}

void BSP_Audio_RequestEffect(BspAudioEffect effect)
{
  if ((uint32_t)effect > (uint32_t)BSP_AUDIO_EFFECT_BOMB) return;

  /* Keep only the newest pending effect.  A newer event is allowed to
     interrupt the currently playing effect immediately.  Protect the
     multi-variable update because this function runs in the game/LVGL task
     while the audio task consumes the request. */
  taskENTER_CRITICAL();
  audio_effect_queue[0U] = (uint8_t)effect;
  audio_effect_tail = 0U;
  audio_effect_head = 1U;
  if (audio_playback_active != 0U) audio_effect_cancel = 1U;
  taskEXIT_CRITICAL();
}

void BSP_Audio_RequestHour(uint8_t hour)
{
  if (hour < 1U || hour > 24U) return;
  /* Hourly chimes are lower priority than game effects.  A game event can
     preempt a chime, while a chime never preempts a game effect. */
  taskENTER_CRITICAL();
  audio_hour_value = hour;
  audio_hour_pending = 1U;
  taskEXIT_CRITICAL();
}

void BSP_Audio_ProcessPending(void)
{
  BspAudioEffect effect;
  uint8_t hour;
  uint8_t is_effect = 0U;
  uint8_t played;

  taskENTER_CRITICAL();
  if (audio_effect_tail != audio_effect_head)
  {
    effect = (BspAudioEffect)audio_effect_queue[audio_effect_tail];
    audio_effect_tail = audio_effect_head;
    is_effect = 1U;
  }
  else if (audio_hour_pending != 0U)
  {
    hour = audio_hour_value;
    audio_hour_pending = 0U;
  }
  else
  {
    taskEXIT_CRITICAL();
    return;
  }

  /* Claim the job before leaving the critical section.  A request arriving
     immediately after this point can therefore see an active playback and
     set audio_effect_cancel without being lost. */
  audio_effect_cancel = 0U;
  audio_playback_active = 1U;
  audio_effect_playing = is_effect;
  taskEXIT_CRITICAL();

  played = (is_effect != 0U) ? BSP_Audio_PlayEffect(effect) :
                              BSP_Audio_PlayHour(hour);

  taskENTER_CRITICAL();
  audio_effect_playing = 0U;
  audio_playback_active = 0U;
  taskEXIT_CRITICAL();

  if (is_effect == 0U || played != 0U)
  {
    audio_effect_retry_count = 0U;
  }
  else if (audio_effect_tail == audio_effect_head &&
           audio_effect_retry_count < 2U &&
           (audio_last_status == 1U || audio_last_status == 5U ||
            audio_last_status == 6U || audio_last_status == 7U))
  {
    /* Retry only transient mount/SD/I2S errors.  A missing or invalid file
       must not trap the default task forever and block later effects. */
    audio_effect_queue[0U] = (uint8_t)effect;
    audio_effect_tail = 0U;
    audio_effect_head = 1U;
    audio_effect_retry_count++;
  }
  else
  {
    audio_effect_retry_count = 0U;
  }
}

uint8_t BSP_Audio_PlayTestVoice(void)
{
  /* File 1 is used only as a hardware-path test.  It does not depend on the
     Time Chime switch or the RTC and therefore isolates audio/SD failures. */
  return BSP_Audio_PlayHour(1U);
}

uint8_t BSP_Audio_GetLastFatFsResult(void)
{
  return audio_last_fresult;
}

uint8_t BSP_Audio_GetLastStatus(void)
{
  return audio_last_status;
}

uint8_t BSP_Audio_GetLastHour(void)
{
  return audio_last_hour;
}

void BSP_TimeChime_SetEnabled(uint8_t enabled)
{
  bsp_time_chime_enabled = (enabled != 0U) ? 1U : 0U;
}

uint8_t BSP_TimeChime_IsEnabled(void)
{
  return bsp_time_chime_enabled;
}

void BSP_TimeChime_SetTimeEditing(uint8_t editing)
{
  bsp_time_chime_time_editing = (editing != 0U) ? 1U : 0U;
}

uint8_t BSP_TimeChime_IsTimeEditing(void)
{
  return bsp_time_chime_time_editing;
}

void BSP_TimeChime_RequestAfterTimeEdit(void)
{
  bsp_time_chime_after_edit_pending = 1U;
}

uint8_t BSP_TimeChime_IsAfterTimeEditPending(void)
{
  return bsp_time_chime_after_edit_pending;
}

void BSP_TimeChime_ClearAfterTimeEdit(void)
{
  bsp_time_chime_after_edit_pending = 0U;
}

void BSP_Board_Init(void)
{
  delay_init();
  BSP_LED_Set(0U, 0U);
  BSP_LED_Set(1U, 0U);
  BSP_Buzzer_Set(0U);
  BSP_Alarm_Init();
  BSP_Touch_Init();
  BSP_GameSerial_Start();
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
