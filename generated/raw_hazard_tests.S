# Auto-generated RAW hazard tests
# XLEN = 32

    .section .text
    .globl _start
_start:
    la x31, signature_area

test_00_add_to_sub:
    # Immediate RAW hazard between ADD and SUB via rs1
    li x5, 9
    li x6, 13
    li x20, 3
    add x10, x5, x6
    sub x14, x10, x20
    sw x14, 0(x31)
    addi x31, x31, 4

test_01_mul_to_xor:
    # Forward MUL result into XOR through rs2 after one cycle
    li x7, 5
    li x8, 12
    li x21, 19
    mul x11, x7, x8
    nop
    xor x15, x21, x11
    sw x15, 0(x31)
    addi x31, x31, 4

test_02_addi_to_add:
    # I-type producer feeds R-type consumer
    li x7, 11
    li x24, 4
    addi x12, x7, 8
    add x16, x12, x24
    sw x16, 0(x31)
    addi x31, x31, 4

test_03_and_to_or:
    # Bitwise chain covering rs2 dependency
    li x19, 26
    li x20, 33
    li x25, 40
    and x13, x19, x20
    or x17, x25, x13
    sw x17, 0(x31)
    addi x31, x31, 4

end_of_tests:
    ebreak
    j end_of_tests

    .section .data
    .balign 4
signature_area:
    .space 16
