require 'minitest/autorun'
require 'lib'

class JudgeTest < Minitest::Test
  def test_fraction
    assert_equal 1.5, Stats.average([1, 2])
  end
end
