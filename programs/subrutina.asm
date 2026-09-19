; Demuestra CALL/RET y calcula 7².
        LOADI 7
        CALL CUADRADO
        OUT
        HALT

CUADRADO:
        SAVE TEMPORAL
        MUL TEMPORAL
        RET

TEMPORAL: .WORD 0

