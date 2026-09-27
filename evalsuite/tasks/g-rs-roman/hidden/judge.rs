use g_rs_roman::*;

#[test]
fn judge_subtractive() {
    assert_eq!(to_roman(4), "IV");
    assert_eq!(to_roman(9), "IX");
    assert_eq!(to_roman(1994), "MCMXCIV");
    assert_eq!(to_roman(3888), "MMMDCCCLXXXVIII");
}
