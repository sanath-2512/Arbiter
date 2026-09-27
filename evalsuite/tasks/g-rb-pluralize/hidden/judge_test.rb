require 'minitest/autorun'
require 'lib'

class JudgeTest < Minitest::Test
  def test_es
    assert_equal 'boxes', Words.pluralize('box', 2)
    assert_equal 'churches', Words.pluralize('church', 3)
    assert_equal 'cats', Words.pluralize('cat', 2)
  end
end
