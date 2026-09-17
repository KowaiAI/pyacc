int call_count = 0;

int fib(int n) {
    call_count++;
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
