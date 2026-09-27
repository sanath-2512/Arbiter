module Words
  def self.pluralize(word, n)
    return word if n == 1

    word.match?(/(s|x|z|ch|sh)\z/) ? word + 'es' : word + 's'
  end
end
