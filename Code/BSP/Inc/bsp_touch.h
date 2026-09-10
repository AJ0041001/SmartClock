#ifndef __BSP_TOUCH_H
#define __BSP_TOUCH_H

#include <stdint.h>

typedef struct {
  uint16_t x;
  uint16_t y;
  uint8_t pressed;
} BspTouchRaw;

void BSP_Touch_Init(void);
uint8_t BSP_Touch_ReadRaw(BspTouchRaw *sample);

#endif
