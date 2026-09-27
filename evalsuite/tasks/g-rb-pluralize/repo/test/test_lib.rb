require 'minitest/autorun'
require 'lib'

class WordsTest < Minitest::Test
  def test_one
    assert_equal 'cat', Words.pluralize('cat', 1)
  end
end
