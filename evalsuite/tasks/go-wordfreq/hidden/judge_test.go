package wordfreq

import (
	"reflect"
	"testing"
)

func TestJudgeTies(t *testing.T) {
	for i := 0; i < 50; i++ {
		got := TopN("b a c a b c d", 2)
		if !reflect.DeepEqual(got, []string{"a", "b"}) {
			t.Fatalf("got %v", got)
		}
	}
	got := TopN("z y x w", 4)
	if !reflect.DeepEqual(got, []string{"w", "x", "y", "z"}) {
		t.Fatalf("got %v", got)
	}
}

func TestJudgeCase(t *testing.T) {
	got := TopN("Go go GO rust", 2)
	if !reflect.DeepEqual(got, []string{"go", "rust"}) {
		t.Fatalf("got %v", got)
	}
	if Count("A a")["a"] != 2 {
		t.Fatalf("count not case-insensitive")
	}
}

func TestJudgeOrder(t *testing.T) {
	got := TopN("a b b c c c", 3)
	if !reflect.DeepEqual(got, []string{"c", "b", "a"}) {
		t.Fatalf("got %v", got)
	}
}
