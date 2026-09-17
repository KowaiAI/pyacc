#include "mathdefs.h"

#define GREETING "preprocessed by accpp"
#define TWICE(n) ((n) + (n))

#ifndef SCALE
#error SCALE should have come from the header
#endif

#if SCALE > 5
#define BIG 1
#else
#define BIG 0
#endif

int helper_double(int n) {
    return TWICE(n);
}

int main(void) {
    printf("%s\n", GREETING);
    printf("SQUARE(7)      = %d\n", SQUARE(7));
    printf("TWICE(21)      = %d\n", TWICE(21));
    printf("SCALE * 4      = %d\n", SCALE * 4);
    printf("BIG            = %d\n", BIG);
    printf("helper_double  = %d\n", helper_double(50));
#ifdef NDEBUG
    printf("release build\n");
#else
    printf("debug build\n");
#endif
    return 0;
}
