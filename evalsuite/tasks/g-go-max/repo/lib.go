package lib

// Max returns the largest value and whether the slice was non-empty.
func Max(xs []int) (int, bool) {
	m := xs[0]
	for _, x := range xs {
		if x > m {
			m = x
		}
	}
	return m, true
}
