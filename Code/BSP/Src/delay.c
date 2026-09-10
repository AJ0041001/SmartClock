#include "delay.h"

void delay_init(void)
{
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CYCCNT = 0U;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

void delay_us(u32 us)
{
  uint32_t start = DWT->CYCCNT;
  uint32_t ticks = us * (SystemCoreClock / 1000000U);
  while ((uint32_t)(DWT->CYCCNT - start) < ticks) { }
}

void delay_ms(u16 ms)
{
  while (ms-- != 0U) {
    delay_us(1000U);
  }
}
