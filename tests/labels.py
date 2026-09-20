"""Element labels and input strings taken from the recorded fixtures.

The fixtures are dumps of Chinese app screens, so the labels the tests assert on
are Chinese. They are collected here, in one file, so that every other module in
the package stays plain ASCII and a reviewer has a single place to check what
non-English text the test suite depends on.
"""

DELIVERY = "外卖"                    # "delivery", the delivery app home channel tile
SEARCH = "搜索"                      # "search"
ORDERS = "订单"                      # "orders"
BURGER_SHOP = "汉堡店"           # text already inside the search field
MILK_TEA = "奶茶"                    # "milk tea", the string typed in tests
