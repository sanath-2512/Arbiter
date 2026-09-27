module Stats
  def self.average(xs)
    return 0 if xs.empty?

    xs.sum.to_f / xs.size
  end
end
