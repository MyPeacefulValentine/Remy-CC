#include "util.h"

int point_weight(struct point value);

int main(void)
{
    struct point origin = {1, 2};
    return point_weight(origin) + add(3, 4);
}
