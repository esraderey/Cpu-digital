/* Demo del puerto propio de FreeRTOS en el hart RV32 (H3.5): un productor manda 1..N por una cola cada 2 tics (vTaskDelay:
 * interrupciones del temporizador y cambios de contexto), un consumidor de mayor prioridad los imprime por el UART y, al
 * acabar, escribe la suma y apaga por SYSCON. El gancho ocioso duerme con WFI (el hart adelanta el reloj hasta el plazo). */
#include "FreeRTOS.h"
#include "queue.h"
#include "task.h"

#include "support.h"

#define ITEMS 20

static StaticQueue_t queue_state;
static uint32_t queue_storage[ 4 ];
static QueueHandle_t queue;
static StaticTask_t producer_tcb, consumer_tcb, idle_tcb;
static StackType_t producer_stack[ 256 ], consumer_stack[ 256 ], idle_stack[ configMINIMAL_STACK_SIZE ];

static void producer( void * parameter )
{
    ( void ) parameter;
    for( uint32_t value = 1; value <= ITEMS; value++ )
    {
        xQueueSend( queue, &value, portMAX_DELAY );
        vTaskDelay( 2 );
    }
    for( ; ; )
    {
        vTaskDelay( 100 );
    }
}

static void consumer( void * parameter )
{
    uint32_t value, sum = 0;
    ( void ) parameter;
    for( uint32_t count = 0; count < ITEMS; count++ )
    {
        xQueueReceive( queue, &value, portMAX_DELAY );
        sum += value;
        uart_puts( "rx " );
        uart_putu( value );
        uart_puts( " tic " );
        uart_putu( xTaskGetTickCount() );
        uart_puts( "\n" );
    }
    uart_puts( "FreeRTOS ok suma " );
    uart_putu( sum );
    uart_puts( "\n" );
    syscon_poweroff();
}

void vApplicationIdleHook( void )
{
    __asm volatile ( "wfi" );
}

void vApplicationGetIdleTaskMemory( StaticTask_t ** tcb, StackType_t ** stack, configSTACK_DEPTH_TYPE * depth )
{
    *tcb = &idle_tcb;
    *stack = idle_stack;
    *depth = configMINIMAL_STACK_SIZE;
}

void vAssertCalled( const char * file, unsigned long line )
{
    uart_puts( "ASSERT " );
    uart_puts( file );
    uart_puts( ":" );
    uart_putu( line );
    uart_puts( "\n" );
    __asm volatile ( "csrw mtvec, zero" );   /* modo compatible del hart: el EBREAK siguiente para la máquina */
    __asm volatile ( "ebreak" );
    for( ; ; )
    {
    }
}

int main( void )
{
    uart_puts( "FreeRTOS " tskKERNEL_VERSION_NUMBER " en el hart RV32\n" );
    queue = xQueueCreateStatic( 4, sizeof( uint32_t ), ( uint8_t * ) queue_storage, &queue_state );
    xTaskCreateStatic( producer, "productor", 256, NULL, 1, producer_stack, &producer_tcb );
    xTaskCreateStatic( consumer, "consumidor", 256, NULL, 2, consumer_stack, &consumer_tcb );
    vTaskStartScheduler();
    vAssertCalled( __FILE_NAME__, __LINE__ );   /* no vuelve nunca */
    return 0;
}
