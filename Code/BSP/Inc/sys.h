#ifndef __ATK_COMPAT_SYS_H
#define __ATK_COMPAT_SYS_H

#include "main.h"
#include <stdint.h>

typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int8_t   s8;
typedef int16_t  s16;
typedef int32_t  s32;
typedef volatile uint8_t  vu8;
typedef volatile uint16_t vu16;
typedef volatile uint32_t vu32;

/* STM32F407 peripheral bit-band aliases, required by the ATK software drivers. */
#define ATK_BITBAND_PERI(addr, bit) (*((volatile uint32_t *)(0x42000000UL + ((((uint32_t)(addr)) - 0x40000000UL) << 5) + ((uint32_t)(bit) << 2))))
#define GPIOA_IDR_ADDR (GPIOA_BASE + 0x10UL)
#define GPIOB_IDR_ADDR (GPIOB_BASE + 0x10UL)
#define GPIOC_IDR_ADDR (GPIOC_BASE + 0x10UL)
#define GPIOF_IDR_ADDR (GPIOF_BASE + 0x10UL)
#define GPIOA_ODR_ADDR (GPIOA_BASE + 0x14UL)
#define GPIOB_ODR_ADDR (GPIOB_BASE + 0x14UL)
#define GPIOC_ODR_ADDR (GPIOC_BASE + 0x14UL)
#define GPIOF_ODR_ADDR (GPIOF_BASE + 0x14UL)
#define PAout(n) ATK_BITBAND_PERI(GPIOA_ODR_ADDR, (n))
#define PBout(n) ATK_BITBAND_PERI(GPIOB_ODR_ADDR, (n))
#define PCout(n) ATK_BITBAND_PERI(GPIOC_ODR_ADDR, (n))
#define PFout(n) ATK_BITBAND_PERI(GPIOF_ODR_ADDR, (n))
#define PAin(n)  ATK_BITBAND_PERI(GPIOA_IDR_ADDR, (n))
#define PBin(n)  ATK_BITBAND_PERI(GPIOB_IDR_ADDR, (n))
#define PCin(n)  ATK_BITBAND_PERI(GPIOC_IDR_ADDR, (n))
#define PFin(n)  ATK_BITBAND_PERI(GPIOF_IDR_ADDR, (n))

#endif
