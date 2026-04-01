🏗️ BẢN CÁO BẠCH KỸ THUẬT VẬN HÀNH (MASTER BLUEPRINT)

Kiến trúc lõi & Cấu hình hệ thống:

· Quy mô vận hành: WorkerPool quản lý 10+ luồng (Workers) chạy song song độc lập.

· Stagger Start (Khởi động so le): Sử dụng random.uniform(12, 25) giây giữa các lần gọi Worker để chống màng lọc nhận diện chu kỳ mạng của Givex.

· Công nghệ lõi: Python + Selenium bọc qua CDP (Chrome DevTools Protocol) và ghost-cursor. Toàn bộ thao tác chuột và phím được đẩy thẳng xuống cấp độ hệ điều hành (OS-level events), đảm bảo cờ isTrusted=True 100%.

· Quản lý Proxy: Sử dụng Proxy tĩnh (SOCKS5/HTTP) map 1-1 với Profile BitBrowser. Tuyệt đối KHÔNG tự động gọi API 9Proxy để lấy IP mới (ngăn chặn triệt để vòng lặp lỗi 402 đốt tiền).

🎬 KỊCH BẢN VẬN HÀNH THỰC TẾ (1 CYCLE)

Góc nhìn trực diện vào tiến trình của Worker #1:

1. Đầu vào & Khởi tạo Worker

Mỗi worker nhận một nhiệm vụ theo định dạng:

email_nhan_the|so_tien|the_16so|exp_thang|exp_nam|cvv

Ví dụ: nguyenvana@yahoo.com|100|4111111111111111|07|27|123

· email_nhan_the: địa chỉ email người nhận thẻ quà tặng (recipient).

· so_tien: mệnh giá thẻ.

· the_16so, exp_thang, exp_nam, cvv: thông tin thẻ thanh toán.

· Các thông tin này được lưu trong suốt cycle.

· OrderQueue của worker chỉ chứa các thẻ (và CVV) cần swap nếu có nhiều hơn một thẻ (tách biệt với thẻ đầu tiên).

2. Khởi động & Tiêm Nhân Cách (00:00 - 00:20)

· Gắn Seed Hành Vi: Worker #1 được cấp một seed nhân cách ngẫu nhiên. Seed này quyết định tốc độ gõ phím, tỷ lệ cố tình gõ sai (mỗi worker có một tỷ lệ riêng, ví dụ 2–5%), và thời gian ngập ngừng (hesitation) giữa các thao tác mục đích mô tả nhiều đối tượng con người như người già, người trẻ, phụ nữ, đàn ông ...

· Kích hoạt BitBrowser & CDP: API BitBrowser được gọi để lấy vân tay (Fingerprint) mới. Trình duyệt mở ra. Trình điều khiển CDP và ghost-cursor lập tức được gắn (attach) vào luồng.

· Tab Janitor (Người Dọn Dẹp): Trình duyệt vừa bật lên kèm 4 tab rác (quảng cáo, trang chủ). Thuật toán quét mảng window_handles, đóng sập toàn bộ tab thừa, chỉ giữ lại đúng 1 tab hiện hành. Tab này được ép tải about:blank và dừng 2 giây để ổn định UI.

· Pre-flight Geo Check: Điều hướng tab duy nhất vào lumtest.com/myip.json. Đọc JSON trả về để xác nhận country: "US". Nếu mạng lag báo lỗi no such window, hệ thống thử lại tối đa 2 lần với khoảng cách 2 giây, kết hợp lệnh switch_to.window để bám sát tab.

3. Xâm nhập & Cách ly Phiên (00:20 - 00:30)

· Khởi tạo & Điều hướng

· URL mục tiêu: https://wwws-usa2.givex.com/cws4.0/lushusa/

· Cookie banner: nếu xuất hiện popup "This Site Uses Cookies", trục chuột ghost-cursor vẽ đường cong Bézier đến nút "OKAY, THANKS" (selector: #button--accept-cookies) và click.

· Vào trang eGift: Click nút Buy E-Gift Cards – Selector: #cardForeground a[href*='Buy-E-gift-Cards']

· Sau đó điều hướng tới URL tạo thẻ: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/

· Hard-Reset State: Selenium thực thi script dọn sạch Cookies, Local Storage và Session Storage ngay lập tức. Giỏ hàng bị ép về trạng thái "trắng", loại trừ 100% rủi ro cộng dồn đơn hàng cũ.

4. Mô Phỏng Sinh Học Trên Form (00:30 - 00:50)

· Cuộn chuột mượt mà (smooth scroll) xuống khu vực điền form e-Gift.

· Điền thông tin nhận thẻ (recipient):

· To (Recipient Email): sử dụng email_nhan_the từ input (không thay đổi trong cycle).

· Recipient Name: lấy từ first_name và last_name của billing profile đã chọn.

· Greeting Message: tự sinh ngẫu nhiên từ danh sách các câu chúc ngắn như “Happy Birthday!”, “Best wishes”, “Enjoy your gift!”, “Thank you for being you”, v.v. (có thể mở rộng).

· From (Sender Name): điền chính xác first_name và last_name của billing profile (giống với Recipient Name, thể hiện người gửi).

· Email billing (thanh toán):

· Lấy từ billing profile đã chọn (first_name.last_name + domain ngẫu nhiên) hoặc dùng email có sẵn trong profile.

· Điền vào ô Billing Email (thường nằm ở khu vực thanh toán, sau khi vào checkout).

· Gõ Phím CDP: Sử dụng lệnh Input.dispatchKeyEvent. Chữ được gõ lên form theo tốc độ của Seed. Quá trình gõ thỉnh thoảng cố tình gõ sai ký tự (theo tỷ lệ riêng của worker), dừng 0.5s, gõ phím Backspace (qua CDP) để xóa và sửa lại đúng.

· Bounding Box Click (Lệch Tâm): Trỏ chuột đến nút "Add to Cart". Tọa độ click được tính bằng thuật toán: tâm của nút cộng trừ ngẫu nhiên (x ± 15, y ± 5). Đảm bảo 10 luồng click vào 10 vị trí khác nhau trên cùng một nút.

· Chờ 3 giây, nút "Review & Checkout" hiện ra. Bot tiếp tục dùng Bounding Box Click để sang trang Giỏ hàng.

5. Bơm Dữ Liệu Thanh Toán (00:50 - 01:20)

· Xác định Zipcode từ Proxy (offline, không gọi API ngoại):

· Sử dụng database MaxMind GeoLite2 (file .mmdb) đã được tải sẵn và nạp vào RAM khi khởi động hệ thống.

· Khi có proxy IP, bot query trực tiếp từ database offline, lấy zipcode trong < 1ms. Không phụ thuộc API bên thứ ba, không rate limit, không rò rỉ IP.

· Lấy Billing từ kho dữ liệu hỗn hợp (có lọc zip & tránh lặp lại):

· Thư mục chứa billing: ./billing_pool/ (đã được đồng bộ local với Google Drive, bot đọc trực tiếp từ local).

· Khi khởi động worker, bot quét tất cả file .txt trong thư mục đó, mỗi dòng là một profile billing với định dạng:

first_name|last_name|address|city|state|zip|phone|email
(các trường có thể thiếu; bot tự sinh bổ sung nếu thiếu).

· Cơ chế chọn billing tránh lặp lại trong cùng worker:

· Toàn bộ profile được load vào danh sách billing_list. Sau đó shuffle ngẫu nhiên danh sách này.

· Worker duy trì một con trỏ (index) trỏ đến vị trí tiếp theo trong danh sách đã shuffle.

· Mỗi khi bắt đầu một cycle mới (với cùng worker), lấy profile tại index hiện tại, sau đó tăng index lên 1 (vòng quanh khi hết danh sách).

· Như vậy, cùng một worker sẽ không dùng lại profile cũ cho đến khi duyệt hết toàn bộ danh sách. Các worker khác có danh sách shuffle riêng.

· Lọc theo zip:

· Khi cần chọn billing cho cycle hiện tại, đầu tiên tìm trong billing_list (theo thứ tự ưu tiên từ vị trí con trỏ trở đi) profile có zip khớp với zip từ MaxMind.

· Nếu tìm thấy profile khớp zip, lấy profile đó và không thay đổi con trỏ (vì con trỏ chỉ dùng cho cơ chế tuần tự mặc định). Có thể dùng thêm một bộ đếm riêng để tránh lặp lại các profile đã dùng gần đây nếu cần.

· Nếu không có profile nào khớp zip, lấy profile tại vị trí con trỏ hiện tại (theo cơ chế tuần tự) và sử dụng zip của profile đó (bỏ qua zip proxy).

· Trường hợp thiếu phone/email trong profile:

· Email: tự sinh theo tên + domain big-tech (nếu thiếu email).

· Phone: sinh số ngẫu nhiên 10 chữ số bắt đầu bằng 2,3,4,5,6,7,8,9 (nếu thiếu phone).

· Billing profile được chọn sẽ được cố định cho toàn bộ cycle (không thay đổi khi swap thẻ).

· Chính sách billing xuyên suốt cycle: Trong toàn bộ vòng đời của một cycle, thông tin billing bao gồm tên, địa chỉ, số điện thoại, email được giữ nguyên không thay đổi. Chỉ có thẻ thanh toán (số thẻ, CVV, ngày hết hạn) được thay đổi mỗi khi swap thẻ ở các ngã rẽ 3 và 4.

· Total Watchdog (Giám sát Tổng Tiền – dùng CDP Network):

· Trước khi điền thẻ, bot kích hoạt CDP Network.enable và lắng nghe sự kiện Network.responseReceived.

· Xác định endpoint API tính tiền/tax (ví dụ /api/checkout/total, /api/tax).

· Khi response trả về status 200 và có dữ liệu tổng tiền, bot mới tiến hành điền thông tin thanh toán.

· Nếu timeout 10 giây không nhận được response, ném lỗi SessionFlaggedError, đóng tab và làm lại phiên mới.

· Quy Tắc Gõ Thẻ 4x4 (Nhìn - Nghĩ - Gõ):

· Thẻ đầu tiên được lấy từ input (của worker). Khi swap thẻ (ngã rẽ 3 hoặc 4), lấy thẻ tiếp theo từ OrderQueue (nếu có).

· Đến trường Credit Card (16 số), bot gọi CDP gõ 4 số đầu -> Khựng lại 0.6s - 1.8s (mô phỏng người dùng đảo mắt nhìn xuống thẻ cứng) -> Gõ tiếp 4 số -> Khựng lại. Cứ thế lặp lại đến hết.

· Hesitation (Ngập ngừng): Điền xong CVV, con trỏ chuột lảng vảng quanh khu vực nút "COMPLETE PURCHASE" khoảng 3 - 5 giây. Cuộn chuột lên xuống nhẹ nhàng để "kiểm tra lại" thông tin, sau đó mới tiến hành click lệch tâm.

6. Gatekeeper & Xử Lý Ngoại Lệ (01:20 - 01:40+)

Lúc này, luồng FSM chia thành 4 ngã rẽ xử lý sự cố thực chiến:

· Ngã rẽ 1: Kẹt UI (Focus-Shift Retry)

· Hiện tượng: Click "Complete Purchase" nhưng vòng xoay loading không chạy, form đơ.

· Xử lý: Đợi 3 giây không phản hồi, chuột lập tức di chuyển ra ngoài form, click vào vùng khoảng trắng (Neutral Div) để kích hoạt sự kiện onBlur giải phóng JS. Sau đó vòng chuột lại tính toán Bounding Box mới và click dứt khoát lần 2.

· Ngã rẽ 2: Success (Thành Công)

· Hiện tượng: URL nhảy sang /confirmation, báo "Thank you for your order".

· Xử lý: Chụp ảnh màn hình. Thuật toán làm mờ ảnh kích hoạt, che kín số thẻ và chỉ để lộ 6 số BIN đầu cùng 4 số cuối (Vd: 411111******1234). Bắn thông báo về Telegram.

· Ngã rẽ 3: VBV/3DS (Iframe Challenge)

· Hiện tượng: URL không đổi, TransientMonitor bắt được tín hiệu Iframe của Adyen/3dsecure bật lên đòi OTP.

· Ràng buộc chờ: Tuyệt đối không refresh, không thao tác với trang chính trong thời gian chờ.

· Xử lý (Dynamic Timeout): Đứng im từ 8 - 12 giây (random) chờ vòng xoay loading của ngân hàng tải xong hoàn toàn bộ khung HTML bên trong Iframe (tránh AI Fraud bắt lỗi hành vi phi nhân loại).

· Click trong Iframe với CDP:

· Switch context vào iframe.

· Lấy tọa độ tương đối của nút "Cancel"/"Return to Merchant"/"X" so với góc trên trái của iframe.

· Lấy tọa độ của iframe so với viewport trang chủ (qua element.getBoundingClientRect()).

· Tính tọa độ tuyệt đối: abs_x = iframe_rect.left + element_rect.left + random_offset, abs_y = iframe_rect.top + element_rect.top + random_offset.

· Dùng CDP Input.dispatchMouseEvent với tọa độ tuyệt đối để click chính xác.

· Xử lý popup rác “Something went wrong” (click nút Close – không xóa DOM):

· Popup này chỉ xuất hiện duy nhất sau khi tắt/skip VBV (hoặc khi trang thanh toán load lại).

· Dùng ghost-cursor hoặc CDP tìm nút "Close", "OK" hoặc "X" trong popup, click vào để kích hoạt đúng chu trình dọn dẹp State của React/Angular.

· Không dùng JavaScript removeNode để tránh desync Virtual DOM.

· Sau khi popup biến mất (state reset), tiến hành xóa form bằng CDP (Ctrl+A + Backspace) và bơm lại thẻ mới theo đúng quy trình, bắt đầu từ bước điền thông tin thanh toán.

· Lưu ý: Khi tắt VBV, site sẽ load lại hoàn toàn trang thanh toán, do đó cần điền lại toàn bộ thông tin (bao gồm thẻ mới và billing address) chứ không chỉ xóa form. Quy trình fill lại tuân thủ đúng kịch bản từ bước “Bơm Dữ Liệu Thanh Toán” trở đi.

· Form trả về trạng thái từ chối (error=vv). Nhảy sang Ngã rẽ 4 nếu vẫn thất bại.

· Ngã rẽ 4: Declined / Transaction Failed (Bơm Thẻ Mới)

· Hiện tượng: Báo "Transaction Declined" hoặc thông báo lỗi từ ngân hàng, billing address trên trang vẫn còn nguyên. (Không có popup che mờ.)

· Zero-Backtrack Soft Reset: TUYỆT ĐỐI KHÔNG TẢI LẠI TRANG (RELOAD).

· Xóa Form bằng CDP: Chuột click vào ô Số Thẻ. Bắn sự kiện CDP nhấn giữ Ctrl + A, sau đó bắn sự kiện Backspace. Form bị xóa trắng tự nhiên, kích hoạt đúng các event validate của React/Angular. Làm tương tự với ô CVV.

· Bơm Thẻ Mới (Next-Card Swap): Bốc thẻ tiếp theo từ OrderQueue. Lặp lại quy tắc gõ 4x4 (Nhìn - Nghĩ - Gõ) và thao tác ngập ngừng trước khi click "COMPLETE PURCHASE" lại từ đầu.

Ràng buộc bộ đếm swap chung:

· Mỗi phiên trình duyệt (mỗi cycle) được phép swap tối đa số thẻ bằng đúng số lượng thẻ có sẵn trong OrderQueue đã cấp cho Worker (không có con số cố định, tùy chỉnh theo file order).

· Bộ đếm swap được reset khi bắt đầu cycle mới (sau khi đóng trình duyệt hoặc khi lấy Profile BitBrowser mới).

· Nếu hết thẻ trong OrderQueue mà chưa có Success, lập tức kết thúc cycle, đóng tab, trả Profile về trạng thái sạch, không thực hiện thêm hành động nào.

· Bộ đếm swap áp dụng thống nhất cho cả hai ngã rẽ 3 và 4: mỗi lần bơm thẻ mới (bất kể từ ngã rẽ nào) đều tăng bộ đếm; khi đạt giới hạn thì dừng cycle.

7. Rút Lui & Xoay Vòng (Cuối Cycle)

· Kết thúc chu trình (báo Success hoặc vứt bỏ sau khi hết thẻ trong queue).

· Đóng sập Tab hiện hành. Thực hiện lệnh xóa Cookies/Cache lần cuối ở cấp độ trình duyệt.

· Trả Profile BitBrowser về trạng thái sạch, tắt luồng.

· Worker quay về đầu OrderQueue, nhận nhiệm vụ mới và bắt đầu một Cycle hoàn toàn mới từ Giai đoạn 0 (Đầu vào & Khởi tạo Worker).

---
