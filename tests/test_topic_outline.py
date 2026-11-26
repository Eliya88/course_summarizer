from src import pipeline


AGENDA = """1. B+Tree
1.1 Simple insert
1.2 Leaf split
2. Hashing
"""


def test_two_level_agenda_numbers_main_and_sub_topics():
    rows = pipeline._number_outline(pipeline._parse_agenda(AGENDA), [])
    assert rows == [("1", "B+Tree"), ("1.1", "Simple insert"), ("1.2", "Leaf split"), ("2", "Hashing")]
    topics = pipeline._written_topics(rows)
    assert [n for n, _, _ in topics] == ["1.1", "1.2", "2"]
    assert topics[0][2] == ["### 1. B+Tree", "#### 1.1 Simple insert"]
    assert topics[2][2] == ["### 2. Hashing"]


def test_later_part_continues_the_main_topic_it_opens_with():
    earlier = [("1", "B+Tree"), ("1.1", "Simple insert"), ("1.2", "Leaf split")]
    rows = pipeline._number_outline(pipeline._parse_agenda("1. B+Tree\n1.1 Delete\n2. Hashing"), earlier)
    assert rows == [("1.3", "Delete"), ("2", "Hashing")]
    assert pipeline._written_topics(rows)[0][2] == ["#### 1.3 Delete"]


def test_continued_main_topic_without_sub_topics_is_not_repeated():
    earlier = [("2", "Delete"), ("2.1", "Merge")]
    rows = pipeline._number_outline(pipeline._parse_agenda("1. Delete\n2. Hashing"), earlier)
    assert rows == [("2.2", pipeline.CONTINUED_TITLE), ("3", "Hashing")]


def test_model_headings_are_replaced_with_the_exact_ones():
    topics = pipeline._written_topics([("1", "B+Tree"), ("1.1", "Insert"), ("1.2", "Split")])
    body = "#### 1.1. Insert\ntext\n##### 1. step one\n### 1.2 Split\nmore"
    assert pipeline._fix_headings(body, topics) == (
        "### 1. B+Tree\n#### 1.1 Insert\ntext\n##### 1. step one\n#### 1.2 Split\nmore")
