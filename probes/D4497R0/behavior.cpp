// Checks the behavior table in D4497R0 ("Summary of behaviors").
// Assumes 8-bit char, 16-bit short, 32-bit int and 64-bit long long.
//
//   c++ -std=c++26 -fsyntax-only probes/D4497R0/behavior.cpp
//   c++ -std=c++26 -fsyntax-only -DD4497_UB=N probes/D4497R0/behavior.cpp
//
// The first command must succeed. With -DD4497_UB=N for N in 1..7 it must
// fail, because that case is undefined or ill-formed and so cannot appear in a
// constant expression.

#include <climits>
#include <compare>
#include <cstdint>
#include <type_traits>
#include <utility>

static_assert(CHAR_BIT == 8 && sizeof(short) == 2 && sizeof(int) == 4 && sizeof(long long) == 8);

// Promotion: the operands become int, so the sum is an int and does not wrap.
static_assert(std::is_same_v<decltype(std::uint8_t{200} + std::uint8_t{100}), int>);
static_assert(std::uint8_t{200} + std::uint8_t{100} == 300);

// Unsigned arithmetic is modular.
static_assert(std::uint32_t{0} - 1u == 4294967295u);

// Increment of a narrow signed type: computed in int, converted back modulo 2^8.
constexpr int increment_int8() { std::int8_t x = 127; ++x; return x; }
static_assert(increment_int8() == -128);

// Division truncates toward zero.
static_assert(7 / -2 == -3 && 7 % -2 == 1);

// Shifts.
static_assert((1 << 31) == INT_MIN);
static_assert((-8 >> 1) == -4);

// Comparisons.
static_assert(!(-1 < 0u));
static_assert(std::cmp_less(-1, 0u));

// Conversions.
static_assert(static_cast<std::int8_t>(200) == -56);

#if D4497_UB == 1
constexpr int a = std::uint16_t{65535} * std::uint16_t{65535};  // int overflow
#elif D4497_UB == 2
constexpr int a = INT_MAX + 1;
#elif D4497_UB == 3
constexpr int a = INT_MIN / -1;
#elif D4497_UB == 4
constexpr int a = 1 << 32;
#elif D4497_UB == 5
constexpr auto a = -1 <=> 0u;  // narrowing: ill-formed
#elif D4497_UB == 6
constexpr int a = static_cast<int>(1e10);
#elif D4497_UB == 7
constexpr int a = 1 / 0;
#endif

int main() {}
