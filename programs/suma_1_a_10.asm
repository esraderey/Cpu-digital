; Suma 1 + 2 + ... + 10 mediante un ciclo.
        LOADI 0
        SAVE TOTAL
        LOADI 10
        SAVE CONTADOR

BUCLE:  LOAD TOTAL
        ADD CONTADOR
        SAVE TOTAL
        LOAD CONTADOR
        SUBI 1
        SAVE CONTADOR
        JNZ BUCLE

        LOAD TOTAL
        OUT
        HALT

TOTAL:     .WORD 0
CONTADOR:  .WORD 0

