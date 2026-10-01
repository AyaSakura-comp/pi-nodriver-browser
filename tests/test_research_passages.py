import unittest
from research import passages


class PassageTests(unittest.TestCase):
    def test_progressive_passages_never_change_committed_prefix(self):
        stream = passages.ProgressivePassages('台積電 收盤價', budget=600)
        header = stream.text
        first = stream.add('s1', '官方', 'https://example.com/1', '台積電 今日收盤價 1200 元。')
        self.assertIn('1200', first)
        second = stream.add('s2', '新聞', 'https://example.com/2', '台積電 昨日收盤價 1190 元。')
        self.assertTrue(stream.text.startswith(header + first))
        self.assertEqual(stream.text, header + first + second)
        self.assertEqual(stream.add('s1', '官方', 'https://example.com/1', '不同內容'), '')
        self.assertEqual(stream.finalize(), header + first + second)

    def test_progressive_passages_bounded_and_ignores_unrelated_page(self):
        stream = passages.ProgressivePassages('台積電 收盤價', budget=200)
        self.assertEqual(stream.add('s1', '無關', 'https://example.com/1', 'spaghetti recipe'), '')
        chunk = stream.add('s2', '相關', 'https://example.com/2', '台積電 收盤價 1200 元。')
        self.assertIn('1200', chunk)
        self.assertLessEqual(stream.used, 200)
        stream.add('s3', '另一篇', 'https://example.com/3', '台積電 收盤價 1100 元。')
        self.assertLessEqual(stream.used, 200)

    def test_finalize_adds_nothing_after_the_crawl(self):
        pages = [(f's{i}', f'Title {i}', f'https://example.com/{i}',
                  '\n'.join(f'台積電 收盤價 第{i}頁 第{k}句 內容說明文字。' for k in range(120))) for i in range(1, 9)]
        stream = passages.ProgressivePassages('台積電 收盤價', budget=1500)
        for p in pages:
            stream.add(*p)
        committed = stream.text
        self.assertEqual(stream.finalize(), committed, 'everything is committed (and prefillable) while crawling')
        self.assertLessEqual(stream.used, 1500)

    def test_slow_top_results_keep_a_reservation_against_early_arrivals(self):
        pages = [(f's{i}', 'T', 'u', '\n'.join(f'台積電 收盤價 {i}-{k} 很多內容。' for k in range(200))) for i in range(1, 30)]
        stream = passages.ProgressivePassages('台積電 收盤價', budget=6000)
        stream.add_results([(sid, 'T', 'u', '') for sid, *_ in pages])
        for p in reversed(pages):   # slow top results arrive last
            stream.add(*p)
        self.assertLessEqual(stream.used, 6000)
        for rank in range(1, 13):
            self.assertIn(f'[s{rank}]', stream.text, 'every top-12 result keeps room for its best passage')

    def test_failed_top_result_releases_its_reservation(self):
        page = '\n'.join(f'台積電 收盤價 內容 {k} 很多說明。' for k in range(200))
        stream = passages.ProgressivePassages('台積電 收盤價', budget=1000, early_rank_limit=2, reserve=500)
        stream.add_results([('s1', 'A', 'u', ''), ('s2', 'B', 'u', '')])
        self.assertEqual(stream.add('s13', 'late', 'u', page), '', 'no room while s1/s2 hold reservations')
        stream.add('s1', 'A', 'u', '')   # s1 failed
        stream.release('s2')
        self.assertIn('[s14]', stream.add('s14', 'late', 'u', page))

    def test_search_snippets_are_committed_first_and_deduplicated(self):
        stream = passages.ProgressivePassages('台積電 收盤價', snippets='commit')
        stream.add_results([('s1', 'A', 'https://a', '台積電 收盤價 2,475'), ('s1', 'A', 'https://a', 'dup'), ('s2', 'B', 'https://b', '')])
        head = stream.text
        self.assertIn('[s1] A — https://a: 台積電 收盤價 2,475', head)
        self.assertNotIn('dup', head); self.assertNotIn('[s2]', head)
        stream.add('s1', 'A', 'https://a', '\n'.join('台積電 收盤價 說明文字很多。' for _ in range(50)))
        self.assertTrue(stream.finalize().startswith(head))

    def test_snippet_locates_the_page_passage_and_is_not_committed_itself(self):
        stream = passages.ProgressivePassages('Mac mini 價格', budget=3000)
        snippet = 'M5 Pro 晶片 NT$56,390 起，教育優惠適用'
        self.assertIn('Top search results', stream.add_results([('s1', 'Apple', 'https://apple', snippet)]),
                      'a top result snippet is committed at search time')
        filler = '\n'.join(f'Mac mini 價格 介紹文字 第{k}段 很多很多內容。' for k in range(80))
        page = filler + '\nM5 Pro 晶片 NT$56,390 起，教育優惠適用於大專院校師生。\n' + filler
        chunk = stream.add('s1', 'Apple', 'https://apple', page)
        self.assertIn('NT$56,390', chunk, 'the passage containing the snippet goes first')

    def test_top_snippets_commit_early_and_low_ranked_snippets_only_on_failure(self):
        stream = passages.ProgressivePassages('台積電 收盤價', budget=3000)
        early = stream.add_results([('s1', 'TWSE', 'https://twse', '台積電 9/29 收盤價 2,475 元'),
                                    ('s20', 'Blog', 'https://blog', '台積電 收盤價 部落格 2,470 元')])
        self.assertIn('2,475', early)
        self.assertNotIn('2,470', early, 'only top-ranked snippets are committed up front')
        self.assertEqual(stream.add('s1', 'TWSE', 'https://twse', ''), '', 'its snippet is already committed')
        self.assertEqual(stream.add('s20', 'Blog', 'https://blog', ''), '', 'late low-ranked failures add nothing')

    def test_early_pages_outside_the_top_results_can_commit_before_top_pages_arrive(self):
        stream = passages.ProgressivePassages('台積電 收盤價', budget=6000)
        stream.add_results([(f's{i}', 'T', 'u', '') for i in range(1, 30)])
        page = '\n'.join(f'台積電 收盤價 內容 {k} 很多說明。' for k in range(60))
        self.assertIn('[s16]', stream.add('s16', 'T', 'u', page), 'half the budget is free for early arrivals')

    def test_top_ranked_page_keeps_a_passage_even_if_others_repeat_keywords(self):
        official = 'Mac mini\nNT$26,590 起\n教育優惠\nM6 晶片 12 核心\nM5 Pro 晶片 NT$56,390 起'
        blogs = [(f's{i}', '\n'.join(['Mac mini 教育價 起價 晶片 Mac mini 教育價 起價 晶片 很多重複的部落格內容。'] * 40)) for i in range(2, 12)]
        got = passages.select([('s1', official)] + blogs, 'Apple 教育商店 Mac mini 起價與晶片', ['Mac mini 教育價'], budget=1500, floor=750)
        self.assertIn('s1', got)
        self.assertIn('NT$56,390', '\n'.join(got['s1']))

    def test_passages_are_verbatim_and_budget_is_respected(self):
        text = '\n'.join(f'Perihelion is in early January and aphelion in early July, sentence {i}.' for i in range(400))
        got = passages.select([('s1', text)], 'when is perihelion and aphelion', budget=600, floor=300)
        joined = '\n'.join(got['s1'])
        self.assertTrue(all(line in text for line in joined.split('\n')))
        self.assertLessEqual(sum(passages.estimate_tokens(p) for p in got['s1']), 600)

    def test_no_keyword_overlap_selects_nothing(self):
        self.assertEqual(passages.select([('s1', 'completely unrelated text about cooking pasta')], '台積電 收盤價'), {})
