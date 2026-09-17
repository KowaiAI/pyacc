int prime_sum = 0;

int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}

int gcd(int a, int b) {
    while (b != 0) {
        int t = b;
        b = a % b;
        a = t;
    }
    return a;
}

int is_prime(int n) {
    if (n < 2) return 0;
    for (int d = 2; d * d <= n; d++) {
        if (n % d == 0) return 0;
    }
    return 1;
}

int main(void) {
    printf("fib(20)        = %d\n", fib(20));
    printf("gcd(1071,462)  = %d\n", gcd(1071, 462));

    int count = 0;
    for (int i = 0; i < 100; i++) {
        if (is_prime(i)) {
            count++;
            prime_sum += i;
        }
    }
    printf("primes < 100   = %d (sum %d)\n", count, prime_sum);

    int *buf = malloc(10 * 8);
    for (int i = 0; i < 10; i++) {
        *(buf + i) = i * i;
    }
    int total = 0;
    for (int i = 0; i < 10; i++) {
        total += *(buf + i);
    }
    free(buf);
    printf("sum of squares = %d\n", total);

    char *s = "compiler";
    int len = strlen(s);
    printf("string         = %s len=%d first=%c last=%c\n", s, len, *s, *(s + len - 1));

    int x = 5;
    int *p = &x;
    *p = *p * 3;
    printf("x through ptr  = %d\n", x);

    int n = 0;
    while (1) {
        n++;
        if (n > 6) break;
        if (n % 2 == 0) continue;
        printf("odd            = %d\n", n);
    }
    return 0;
}
