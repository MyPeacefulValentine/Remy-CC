#ifndef ORACLE_FIXTURE_UTIL_H
#define ORACLE_FIXTURE_UTIL_H

struct point {
    int x;
    int y;
};

enum color {
    COLOR_RED,
    COLOR_BLUE
};

typedef int (*combine_fn)(int, int);

int add(int left, int right);

#endif
