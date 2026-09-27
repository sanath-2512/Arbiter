require 'minitest/autorun'
require 'lib'

class StatsTest < Minitest::Test
  def test_empty
    assert_equal 0, Stats.average([])
  end
end
