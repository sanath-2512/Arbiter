use g_rs_fahrenheit::*;

#[test]
fn judge_fraction() {
    assert!((c_to_f(37.5) - 99.5).abs() < 1e-9);
    assert!((c_to_f(-40.0) + 40.0).abs() < 1e-9);
    assert!((c_to_f(1.0) - 33.8).abs() < 1e-9);
}
