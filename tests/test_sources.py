from pa.scout.sources import classify_source


def test_classify():
    assert classify_source("https://blog.naver.com/x/1") == "review"
    assert classify_source("https://map.naver.com/p/entry/place/1") == "map"
    assert classify_source("https://www.instagram.com/p/1") == "sns"
    assert classify_source("https://www.gwanak.go.kr/park") == "official"
    assert classify_source("https://cafe-official.kr/menu", official_domains={"cafe-official.kr"}) == "official"
    assert classify_source("https://random.example/x") == "other"
