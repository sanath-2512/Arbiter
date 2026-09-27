module Words
  def self.pluralize(word, n)
    return word if n == 1

    word + 's'
  end
end
