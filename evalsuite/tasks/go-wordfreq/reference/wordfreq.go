// Package wordfreq counts word frequencies.
package wordfreq

import (
	"sort"
	"strings"
)

// Entry is a word and how often it occurs.
type Entry struct {
	Word  string
	Count int
}

// Count returns the frequency of each whitespace-separated word.
func Count(text string) map[string]int {
	counts := map[string]int{}
	for _, w := range strings.Fields(text) {
		counts[strings.ToLower(w)]++
	}
	return counts
}

// TopN returns the n most frequent words, most frequent first.
func TopN(text string, n int) []string {
	var entries []Entry
	for w, c := range Count(text) {
		entries = append(entries, Entry{w, c})
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].Count != entries[j].Count {
			return entries[i].Count > entries[j].Count
		}
		return entries[i].Word < entries[j].Word
	})
	var out []string
	for i := 0; i < n && i < len(entries); i++ {
		out = append(out, entries[i].Word)
	}
	return out
}
