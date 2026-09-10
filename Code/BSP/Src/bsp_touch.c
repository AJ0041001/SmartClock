#include "../Inc/bsp_touch.h"
#include "main.h"
#include "delay.h"

static void touch_write_byte(uint8_t data)
{
  for (uint8_t bit = 0U; bit < 8U; bit++) {
    HAL_GPIO_WritePin(T_MOSI_GPIO_Port, T_MOSI_Pin, (data & 0x80U) ? GPIO_PIN_SET : GPIO_PIN_RESET);
    data <<= 1U;
    HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_RESET);
    delay_us(1U);
    HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_SET);
    delay_us(1U);
  }
}

static uint16_t touch_read_ad(uint8_t command)
{
  uint16_t value = 0U;
  HAL_GPIO_WritePin(T_CS_GPIO_Port, T_CS_Pin, GPIO_PIN_RESET);
  HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_RESET);
  touch_write_byte(command);
  delay_us(8U);
  for (uint8_t bit = 0U; bit < 16U; bit++) {
    HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_RESET);
    delay_us(1U);
    HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_SET);
    value = (uint16_t)((value << 1U) | (HAL_GPIO_ReadPin(T_MISO_GPIO_Port, T_MISO_Pin) == GPIO_PIN_SET));
    delay_us(1U);
  }
  HAL_GPIO_WritePin(T_CS_GPIO_Port, T_CS_Pin, GPIO_PIN_SET);
  return (uint16_t)(value >> 4U);
}

static uint16_t touch_filtered(uint8_t command)
{
  uint16_t values[5];
  uint32_t sum = 0U;
  for (uint8_t i = 0U; i < 5U; i++) values[i] = touch_read_ad(command);
  for (uint8_t i = 0U; i < 4U; i++) {
    for (uint8_t j = i + 1U; j < 5U; j++) {
      if (values[i] > values[j]) { uint16_t temp = values[i]; values[i] = values[j]; values[j] = temp; }
    }
  }
  for (uint8_t i = 1U; i < 4U; i++) sum += values[i];
  return (uint16_t)(sum / 3U);
}

void BSP_Touch_Init(void)
{
  HAL_GPIO_WritePin(T_CS_GPIO_Port, T_CS_Pin, GPIO_PIN_SET);
  HAL_GPIO_WritePin(T_SCK_GPIO_Port, T_SCK_Pin, GPIO_PIN_RESET);
}

uint8_t BSP_Touch_ReadRaw(BspTouchRaw *sample)
{
  if (sample == NULL) return 0U;
  sample->pressed = (HAL_GPIO_ReadPin(T_PEN_GPIO_Port, T_PEN_Pin) == GPIO_PIN_RESET);
  if (sample->pressed == 0U) return 0U;
  sample->x = touch_filtered(0xD0U);
  sample->y = touch_filtered(0x90U);
  return 1U;
}
