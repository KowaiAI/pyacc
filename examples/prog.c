int fib(int n);
int gcd(int a, int b);
extern int call_count;

int main(void) {
    printf("linked across two objects\n");
    printf("  fib(15)       = %d\n", fib(15));
    printf("  fib call count= %d\n", call_count);
    printf("  gcd(252, 105) = %d\n", gcd(252, 105));
    return 0;
}
