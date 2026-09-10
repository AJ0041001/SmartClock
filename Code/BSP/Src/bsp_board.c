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

static uint16_t adc_samples[128];
static uint16_t dac_samples[128];
static uint16_t audio_samples[128];
static uint8_t adc_started;
static uint8_t audio_started;

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

void BSP_Buzzer_Beep(uint32_t milliseconds)
{
  HAL_GPIO_WritePin(BEEP_GPIO_Port, BEEP_Pin, GPIO_PIN_SET);
  HAL_Delay(milliseconds);
  HAL_GPIO_WritePin(BEEP_GPIO_Port, BEEP_Pin, GPIO_PIN_RESET);
}

uint8_t BSP_Key_Read(void)
{
  if (HAL_GPIO_ReadPin(KEY0_GPIO_Port, KEY0_Pin) == GPIO_PIN_RESET) return 1U;
  if (HAL_GPIO_ReadPin(KEY1_GPIO_Port, KEY1_Pin) == GPIO_PIN_RESET) return 2U;
  if (HAL_GPIO_ReadPin(KEY2_GPIO_Port, KEY2_Pin) == GPIO_PIN_RESET) return 3U;
  if (HAL_GPIO_ReadPin(KEY_UP_GPIO_Port, KEY_UP_Pin) == GPIO_PIN_SET) return 4U;
  return 0U;
}

uint16_t BSP_ADC_Average(void)
{
  uint32_t sum = 0U;
  if (adc_started == 0U) {
    if (HAL_ADC_Start_DMA(&hadc1, (uint32_t *)adc_samples, 128U) == HAL_OK) adc_started = 1U;
    return 0U;
  }
  for (uint32_t i = 0U; i < 128U; i++) sum += adc_samples[i];
  return (uint16_t)(sum / 128U);
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
  HAL_GPIO_WritePin(BEEP_GPIO_Port, BEEP_Pin, GPIO_PIN_RESET);
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
