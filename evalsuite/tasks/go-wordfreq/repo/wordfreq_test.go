package wordfreq

import "testing"

func TestCount(t *testing.T) {
	c := Count("x y x")
	if c["x"] != 2 || c["y"] != 1 {
		t.Fatalf("unexpected counts %v", c)
	}
}
