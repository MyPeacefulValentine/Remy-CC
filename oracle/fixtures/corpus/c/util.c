#include "util.h"

/* Sum two coordinates. */
int add(int left, int right)
{
    return left + right;
}

int point_weight(struct point value)
{
    return add(value.x, value.y);
}
