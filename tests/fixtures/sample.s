#include "constants/things.h"
	.include "macros.inc"

	.text

	thumb_func_start Foo
Foo: ; 0x02100010
	push {r4, lr}
	ldr r0, _02100024 ; =0x00000A54
#ifdef VERSION_A
	mov r1, #2
#else
	mov r1, #5
#endif
	pop {r4, pc}
	.balign 4, 0
_02100024: .word 0x00000A54
	thumb_func_end Foo

	arm_func_start Bar
Bar: ; 0x02100028
	bx lr
	arm_func_end Bar
