package lib

// SumPositive adds the values greater than zero.
func SumPositive(xs []int) int {
	total := 0
	for _, x := range xs {
		total += x
	}
	return total
}
