module Stats
  def self.average(xs)
    return 0 if xs.empty?

    xs.sum / xs.size
  end
end
