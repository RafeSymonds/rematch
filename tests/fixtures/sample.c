#include "global.h"
#include "foo.h"

#define SQUARE(x) ((x) * (x))

typedef struct Thing {
    int a; /* { not a body } */
    char *s;
} Thing;

static const char *sMessage = "brace { in string";
static int sTable[] = { 1, 2, 3 };

static inline int Helper(int x) {
    return SQUARE(x);
}

static void Local(Thing *t);

int Target(Thing *t, int n) {
    if (n > 0) {
        return Helper(t->a) + sTable[n];
    }
    // closing brace in comment }
    return '}';
}

static void Local(Thing *t)
{
    t->a = 0;
}

void (*GetCallback(void))(int) {
    return 0;
}
