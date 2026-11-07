import unittest

from research import passages
from research.passages import ProgressivePassages


class PassageTests(unittest.TestCase):
    def test_committed_prefix_never_changes(self):
        stream = ProgressivePassages('台積電 收盤價', budget=600)
        header = stream.text
        first = stream.add('s1', '官方', 'https://example.com/1', '台積電 今日收盤價 1200 元。')
        self.assertIn('1200', first)
        second = stream.add('s2', '新聞', 'https://example.com/2', '台積電 昨日收盤價 1190 元。')
        self.assertEqual(stream.text, header + first + second)
        self.assertEqual(stream.add('s1', '官方', 'https://example.com/1', '不同內容'), '', 'a page commits once')
        self.assertEqual(stream.delivered, {'s1', 's2'})

    def test_budget_is_bounded_and_an_unrelated_page_adds_nothing(self):
        stream = ProgressivePassages('台積電 收盤價', budget=200)
        self.assertEqual(stream.add('s1', '無關', 'https://example.com/1', 'spaghetti recipe'), '')
        self.assertIn('1200', stream.add('s2', '相關', 'https://example.com/2', '台積電 收盤價 1200 元。'))
        stream.add('s3', '另一篇', 'https://example.com/3', '台積電 收盤價 1100 元。')
        self.assertLessEqual(stream.used, 200)

    def test_long_pages_stay_within_the_budget(self):
        stream = ProgressivePassages('台積電 收盤價', budget=1500)
        for i in range(1, 9):
            stream.add(f's{i}', f'Title {i}', f'https://example.com/{i}',
                       '\n'.join(f'台積電 收盤價 第{i}頁 第{k}句 內容說明文字。' for k in range(120)))
        self.assertLessEqual(stream.used, 1500)

    def test_slow_top_results_keep_a_reservation_against_early_arrivals(self):
        pages = [(f's{i}', 'T', 'u', '\n'.join(f'台積電 收盤價 {i}-{k} 很多內容。' for k in range(200))) for i in range(1, 30)]
        stream = ProgressivePassages('台積電 收盤價', budget=6000)
        stream.add_results([(sid, 'T', 'u', '') for sid, *_ in pages])
        for page in reversed(pages):  # slow top results arrive last
            stream.add(*page)
        self.assertLessEqual(stream.used, 6000)
        for rank in range(1, 13):
            self.assertIn(f'[s{rank}]', stream.text, 'every top-12 result keeps room for its best passage')

    def test_failed_top_results_release_their_reservation(self):
        stream = ProgressivePassages('台積電 收盤價', budget=2400)  # 12 top results reserve 100 tokens each
        stream.add_results([(f's{i}', 'T', 'u', '') for i in range(1, 13)])
        self.assertNotIn('Top search results', stream.text, 'empty snippets commit nothing')
        page = '台積電收盤價' * 216  # one ~1.5k-token sentence: it fits only without the reservations
        self.assertEqual(stream.add('s13', 'late', 'u', page), '', 'no room while s1..s12 hold reservations')
        for i in range(1, 13):
            stream.add(f's{i}', 'T', 'u', '')  # every top page failed
        self.assertIn('[s14]', stream.add('s14', 'late', 'u', page))

    def test_early_pages_outside_the_top_results_can_commit_before_top_pages_arrive(self):
        stream = ProgressivePassages('台積電 收盤價', budget=6000)
        stream.add_results([(f's{i}', 'T', 'u', '') for i in range(1, 30)])
        page = '\n'.join(f'台積電 收盤價 內容 {k} 很多說明。' for k in range(60))
        self.assertIn('[s16]', stream.add('s16', 'T', 'u', page), 'half the budget is free for early arrivals')

    def test_snippet_locates_the_page_passage_and_is_committed_once(self):
        stream = ProgressivePassages('Mac mini 價格', budget=3000)
        snippet = 'M5 Pro 晶片 NT$56,390 起，教育優惠適用'
        early = stream.add_results([('s1', 'Apple', 'https://apple', snippet), ('s1', 'Apple', 'https://apple', 'dup')])
        self.assertIn('Top search results', early, 'a top result snippet is committed at search time')
        self.assertNotIn('dup', stream.text)
        filler = '\n'.join(f'Mac mini 價格 介紹文字 第{k}段 很多很多內容。' for k in range(80))
        chunk = stream.add('s1', 'Apple', 'https://apple', filler + '\nM5 Pro 晶片 NT$56,390 起，教育優惠適用於大專院校師生。\n' + filler)
        self.assertIn('NT$56,390', chunk, 'the passage containing the snippet goes first')

    def test_top_snippets_commit_early_and_low_ranked_snippets_never(self):
        stream = ProgressivePassages('台積電 收盤價', budget=3000)
        early = stream.add_results([('s1', 'TWSE', 'https://twse', '台積電 9/29 收盤價 2,475 元'),
                                    ('s20', 'Blog', 'https://blog', '台積電 收盤價 部落格 2,470 元')])
        self.assertIn('2,475', early)
        self.assertNotIn('2,470', early, 'only top-ranked snippets are committed up front')
        self.assertEqual(stream.add('s1', 'TWSE', 'https://twse', ''), '', 'its snippet is already committed')
        self.assertEqual(stream.add('s20', 'Blog', 'https://blog', ''), '', 'late low-ranked failures add nothing')

    def test_rough_word_count_needs_no_tokenizer(self):
        self.assertEqual(passages.rough_words('hello world'), 2)
        self.assertEqual(passages.rough_words('中文測試'), 4)
        self.assertEqual(passages.rough_words(''), 0)
        self.assertGreater(passages.rough_words('x' * 800), 1)
        self.assertGreater(passages.rough_words('+' * 800), 1)


if __name__ == '__main__':
    unittest.main()
