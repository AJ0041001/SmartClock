#ifndef __BSP_DELAY_H
#define __BSP_DELAY_H

#include "sys.h"

void delay_init(void);
void delay_us(u32 us);
void delay_ms(u16 ms);

#endif
