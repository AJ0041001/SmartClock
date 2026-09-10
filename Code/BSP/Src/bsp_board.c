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
  for (uint32_t i = 0U; i < 128U; i++) dac_samples[i] = (i < 64U) ? 3072U : 1024U;
  (void)HAL_DAC_Stop_DMA(&hdac, DAC_CHANNEL_1);
  (void)HAL_DAC_Start_DMA(&hdac, DAC_CHANNEL_1, (uint32_t *)dac_samples, 128U, DAC_ALIGN_12B_R);
  (void)HAL_TIM_Base_Start(&htim6);
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
