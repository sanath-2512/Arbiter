pub fn to_roman(mut n: u32) -> String {
    let table = [(1000, "M"), (500, "D"), (100, "C"), (50, "L"), (10, "X"), (5, "V"), (1, "I")];
    let mut out = String::new();
    for (value, sym) in table {
        while n >= value {
            out.push_str(sym);
            n -= value;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple() {
        assert_eq!(to_roman(3), "III");
    }
}
