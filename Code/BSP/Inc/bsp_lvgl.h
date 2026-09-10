#ifndef __BSP_LVGL_H
#define __BSP_LVGL_H

#include "lvgl.h"

void BSP_LVGL_Init(void);
void BSP_LVGL_Task(void *argument);
void BSP_LVGL_TickInc(void);

#endif
