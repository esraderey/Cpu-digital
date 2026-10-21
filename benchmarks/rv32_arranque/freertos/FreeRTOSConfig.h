/* FreeRTOSConfig.h del puerto propio de FreeRTOS para el hart RV32 de RFC-EXP-00018 (H3.5): modo M, RV32IMA_Zicsr, CLINT
 * de la plataforma (mtime en 0x1100BFF8, mtimecmp en 0x11004000), mtime a 1 MHz nominal (un tic = 1 µs del invitado; con
 * divisor 1, una instrucción por tic) y un tic de FreeRTOS cada 1 000 tics de mtime. Todo estático: sin montículo. */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configMTIME_BASE_ADDRESS                ( 0x1100BFF8UL )
#define configMTIMECMP_BASE_ADDRESS             ( 0x11004000UL )
#define configCPU_CLOCK_HZ                      ( 1000000UL )   /* frecuencia de mtime: la del DTB (timebase-frequency) */
#define configTICK_RATE_HZ                      ( ( TickType_t ) 1000 )
#define configISR_STACK_SIZE_WORDS              ( 256 )

#define configUSE_PREEMPTION                    1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#define configMAX_PRIORITIES                    ( 4 )
#define configMINIMAL_STACK_SIZE                ( ( unsigned short ) 256 )
#define configMAX_TASK_NAME_LEN                 ( 12 )
#define configTICK_TYPE_WIDTH_IN_BITS           TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD                 1
#define configUSE_IDLE_HOOK                     1               /* el gancho ocioso duerme con WFI */
#define configUSE_TICK_HOOK                     0
#define configUSE_MUTEXES                       0
#define configUSE_RECURSIVE_MUTEXES             0
#define configUSE_COUNTING_SEMAPHORES           0
#define configQUEUE_REGISTRY_SIZE               0
#define configUSE_TIMERS                        0
#define configUSE_CO_ROUTINES                   0
#define configUSE_TRACE_FACILITY                0
#define configUSE_STATS_FORMATTING_FUNCTIONS    0
#define configGENERATE_RUN_TIME_STATS           0
#define configCHECK_FOR_STACK_OVERFLOW          0
#define configUSE_MALLOC_FAILED_HOOK            0
#define configSUPPORT_STATIC_ALLOCATION         1
#define configSUPPORT_DYNAMIC_ALLOCATION        0
#define configKERNEL_INTERRUPT_PRIORITY         0
#define configMAX_SYSCALL_INTERRUPT_PRIORITY    0

#define INCLUDE_vTaskDelay                      1
#define INCLUDE_vTaskDelete                     0
#define INCLUDE_vTaskSuspend                    0
#define INCLUDE_xTaskGetSchedulerState          0

void vAssertCalled( const char * file, unsigned long line );
#define configASSERT( x )    if( ( x ) == 0 ) vAssertCalled( __FILE_NAME__, __LINE__ )   /* sin rutas: binario reproducible */

#endif /* FREERTOS_CONFIG_H */
