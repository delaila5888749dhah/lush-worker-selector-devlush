🏗️ BẢN CÁO BẠCH KỸ THUẬT VẬN HÀNH (MASTER BLUEPRINT)

Kiến trúc lõi & Cấu hình hệ thống:

· Quy mô vận hành: WorkerPool quản lý 10+ luồng (Workers) chạy song song độc lập.(Có thể nâng cấp mạnh về sau)

· Stagger Start (Khởi động so le): Sử dụng random.uniform(12, 25) giây giữa các lần gọi Worker để chống màng lọc nhận diện chu kỳ mạng của Givex.

· Công nghệ lõi: Python + Selenium bọc qua CDP (Chrome DevTools Protocol) và ghost-cursor. Toàn bộ thao tác chuột và phím được đẩy thẳng xuống cấp độ hệ điều hành (OS-level events), đảm bảo cờ isTrusted=True 100%.

· Quản lý Proxy: Sử dụng Proxy tĩnh (SOCKS5/HTTP) map 1-1 với Profile trình duyệt Antidetect BitBrowser. Tuyệt đối KHÔNG tự động gọi API 9Proxy để lấy IP mới (ngăn chặn triệt để vòng lặp lỗi 402 đốt tiền).

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

· Cookie banner: nếu xuất hiện popup "This Site Uses Cookies", trục chuột ghost-cursor vẽ đường cong Bézier đến nút "Accept cookies" và click.
  Selector: #button--accept-cookies

· Vào trang eGift: Click nút Buy E-Gift Cards.
  Selector: #cardForeground > div > div.bannerButtons.clearfix > div.bannerBtn.btn1.displaySectionYes > a

· Sau đó điều hướng tới URL tạo thẻ: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/

· Hard-Reset State: Selenium thực thi script dọn sạch Cookies, Local Storage và Session Storage ngay lập tức. Giỏ hàng bị ép về trạng thái "trắng", loại trừ 100% rủi ro cộng dồn đơn hàng cũ.

4. Mô Phỏng Sinh Học Trên Form (00:30 - 00:50)

· URL mục tiêu: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/

· Cuộn chuột mượt mà (smooth scroll) xuống khu vực điền form e-Gift.

· Điền thông tin nhận thẻ (recipient):

· Greeting Message: tự sinh ngẫu nhiên từ danh sách các câu chúc ngắn như "Happy Birthday!", "Best wishes", "Enjoy your gift!", "Thank you for being you", v.v. (có thể mở rộng).
  Selector: #cws_txt_gcMsg

· Amount (Mệnh giá thẻ): sử dụng so_tien từ input.
  Selector: #cws_txt_gcBuyAmt

· To (Recipient Name): lấy từ first_name và last_name của billing profile đã chọn.
  Selector: #cws_txt_gcBuyTo

· Recipient Email: sử dụng email_nhan_the từ input (không thay đổi trong cycle).
  Selector: #cws_txt_recipEmail

· Confirm Recipient Email: nhập lại chính xác email_nhan_the.
  Selector: #cws_txt_confRecipEmail

· From (Sender Name): điền chính xác first_name và last_name của billing profile (giống với Recipient Name, thể hiện người gửi).
  Selector: #cws_txt_gcBuyFrom

· Email billing (thanh toán):

· Lấy từ billing profile đã chọn (first_name.last_name + domain ngẫu nhiên) hoặc dùng email có sẵn trong profile.

· Điền vào ô Billing Email (thường nằm ở khu vực thanh toán, sau khi vào checkout).

· Gõ Phím CDP: Sử dụng lệnh Input.dispatchKeyEvent. Chữ được gõ lên form theo tốc độ của Seed. Quá trình gõ thỉnh thoảng cố tình gõ sai ký tự (theo tỷ lệ riêng của worker), dừng 0.5s, gõ phím Backspace (qua CDP) để xóa và sửa lại đúng.

· Bounding Box Click (Lệch Tâm): Trỏ chuột đến nút "Add to Cart". Tọa độ click được tính bằng thuật toán: tâm của nút cộng trừ ngẫu nhiên (x ± 15, y ± 5). Đảm bảo 10 luồng click vào 10 vị trí khác nhau trên cùng một nút.
  Selector: #cws_btn_gcBuyAdd > span

· Chờ 3 giây, nút "Review & Checkout" hiện ra. Bot tiếp tục dùng Bounding Box Click để sang trang Giỏ hàng.
  Selector: #cws_btn_gcBuyCheckout

5. Bơm Dữ Liệu Thanh Toán (00:50 - 01:20)

· Cart & Guest Checkout:

  · URL giỏ hàng: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html

  · Click BEGIN CHECKOUT.
    Selector: #cws_btn_cartCheckout

  · URL checkout: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html

  · Nhập Guest Email.
    Selector: #cws_txt_guestEmail

  · Click CONTINUE.
    Selector: #cws_btn_guestChkout

· URL trang thanh toán: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html

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

· Điền thông tin thẻ thanh toán (Payment Fields):

  · Name Shown on Card.
    Selector: #cws_txt_ccName

  · Card Number (16 số).
    Selector: #cws_txt_ccNum

  · Expiry Date Month.
    Selector: #cws_list_ccExpMon

  · Expiry Date Year.
    Selector: #cws_list_ccExpYr

  · CVV Number.
    Selector: #cws_txt_ccCvv

· Điền thông tin Billing Address:

  · Address 1.
    Selector: #cws_txt_billingAddr1

  · Country.
    Selector: #cws_list_billingCountry

  · State/Province.
    Selector: #cws_list_billingProvince

  · City.
    Selector: #cws_txt_billingCity

  · Zip/Postal Code.
    Selector: #cws_txt_billingPostal

  · Phone Number.
    Selector: #cws_txt_billingPhone

· Quy Tắc Gõ Thẻ 4x4 (Nhìn - Nghĩ - Gõ):

· Thẻ đầu tiên được lấy từ input (của worker). Khi swap thẻ (ngã rẽ 3 hoặc 4), lấy thẻ tiếp theo từ OrderQueue (nếu có).

· Đến trường Credit Card (16 số) (selector: #cws_txt_ccNum), bot gọi CDP gõ 4 số đầu -> Khựng lại 0.6s - 1.8s (mô phỏng người dùng đảo mắt nhìn xuống thẻ cứng) -> Gõ tiếp 4 số -> Khựng lại. Cứ thế lặp lại đến hết.

· Hesitation (Ngập ngừng): Điền xong CVV (selector: #cws_txt_ccCvv), con trỏ chuột lảng vảng quanh khu vực nút "COMPLETE PURCHASE" (selector: #cws_btn_checkoutPay) khoảng 3 - 5 giây. Cuộn chuột lên xuống nhẹ nhàng để "kiểm tra lại" thông tin, sau đó mới tiến hành click lệch tâm.

· Hoàn tất: Kiểm tra Order Total, click COMPLETE PURCHASE.
  Selector: #cws_btn_checkoutPay

6. Gatekeeper & Xử Lý Ngoại Lệ (01:20 - 01:40+)

Lúc này, luồng FSM chia thành 4 ngã rẽ xử lý sự cố thực chiến:

· Ngã rẽ 1: Kẹt UI (Focus-Shift Retry)

· Hiện tượng: Click "Complete Purchase" (selector: #cws_btn_checkoutPay) nhưng vòng xoay loading không chạy, form đơ.

· Xử lý: Đợi 3 giây không phản hồi, chuột lập tức di chuyển ra ngoài form, click vào vùng khoảng trắng (Neutral Div) để kích hoạt sự kiện onBlur giải phóng JS. Sau đó vòng chuột lại tính toán Bounding Box mới và click dứt khoát lần 2 (selector: #cws_btn_checkoutPay).

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

· Xử lý popup rác "Something went wrong" (click nút Close – không xóa DOM):

· Popup này chỉ xuất hiện duy nhất sau khi tắt/skip VBV (hoặc khi trang thanh toán load lại).

· Dùng ghost-cursor hoặc CDP tìm nút "Close", "OK" hoặc "X" trong popup, click vào để kích hoạt đúng chu trình dọn dẹp State của React/Angular.

· Không dùng JavaScript removeNode để tránh desync Virtual DOM.

· Sau khi popup biến mất (state reset), tiến hành xóa form bằng CDP (Ctrl+A + Backspace) và bơm lại thẻ mới theo đúng quy trình, bắt đầu từ bước điền thông tin thanh toán.

· Lưu ý: Khi tắt VBV, site sẽ load lại hoàn toàn trang thanh toán (URL: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html), do đó cần điền lại toàn bộ thông tin (bao gồm thẻ mới và billing address) chứ không chỉ xóa form. Quy trình fill lại tuân thủ đúng kịch bản từ bước "Bơm Dữ Liệu Thanh Toán" trở đi.

· Form trả về trạng thái từ chối (error=vv). Nhảy sang Ngã rẽ 4 nếu vẫn thất bại.

· Ngã rẽ 4: Declined / Transaction Failed (Bơm Thẻ Mới)

· Hiện tượng: Báo "Transaction Declined" hoặc thông báo lỗi từ ngân hàng, billing address trên trang vẫn còn nguyên. (Không có popup che mờ.)

· Zero-Backtrack Soft Reset: TUYỆT ĐỐI KHÔNG TẢI LẠI TRANG (RELOAD).

· Xóa Form bằng CDP: Chuột click vào ô Số Thẻ (selector: #cws_txt_ccNum). Bắn sự kiện CDP nhấn giữ Ctrl + A, sau đó bắn sự kiện Backspace. Form bị xóa trắng tự nhiên, kích hoạt đúng các event validate của React/Angular. Làm tương tự với ô CVV.

· Bơm Thẻ Mới (Next-Card Swap): Bốc thẻ tiếp theo từ OrderQueue. Lặp lại quy tắc gõ 4x4 (Nhìn - Ngh�� - Gõ) và thao tác ngập ngừng trước khi click "COMPLETE PURCHASE" (selector: #cws_btn_checkoutPay) lại từ đầu.

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

8. KIẾN TRÚC HÀNH VI — PHASE 10 BEHAVIOR LAYER (BEHAVIOR ARCHITECTURE)

Mapping giữa Blueprint và Phase 10 Behavior Layer.

· PersonaProfile (Nhân Cách Worker):

  · Nguồn gốc: Seed Hành Vi được cấp tại §2 (Khởi động & Tiêm Nhân Cách).

  · Bao gồm:
    - typing_speed: tốc độ gõ phím (quy định bởi seed, §4 quy tắc 4x4: 0.6–1.8s mỗi nhóm 4 số)
    - typo_rate: tỷ lệ cố tình gõ sai (2–5% theo seed, §4)
    - hesitation_pattern: thời gian ngập ngừng giữa các thao tác (§5: 3–5s)
    - persona_type: mô tả đối tượng (người già, trẻ, phụ nữ, đàn ông — §2)

  · Vòng đời: Cố định suốt cycle. Không thay đổi khi swap thẻ.

· AntiDetection Layer:

  · Tầng điều khiển hành vi theo thời gian (temporal behavior control).
  · Hoạt động song song với PersonaProfile để tạo biometrics giống người thật.
  · Chi tiết xem §9 (Anti-Detect Layer 2 Tầng).

---

§8.1. TÍCH HỢP THỰC THI — ARCHITECTURE (↔ Spec §10.1)

· Cơ chế: Behavior được inject tại worker execution layer thông qua pattern wrapper:

  task_fn = wrap(task_fn, persona)

· Vị trí inject: Bên trong worker function, bao bọc task_fn gốc.

· KHÔNG can thiệp vào:
  - Runtime loop (vòng lặp điều khiển)
  - Rollout / Scaling (quản lý số lượng worker)
  - Monitor (giám sát metrics)
  - FSM (máy trạng thái — flow §6 giữ nguyên 100%)

---

§8.2. FSM CONTEXT — BEHAVIORSTATE (↔ Spec §10.2)

· Theo dõi ngữ cảnh hiện tại của worker trong cycle:
  - IDLE — chờ bước tiếp theo (giữa các thao tác)
  - FILLING_FORM — đang điền form (recipient, billing — §4)
  - PAYMENT — đang nhập thẻ thanh toán (card number, CVV — §5)
  - VBV — đang xử lý 3DS iframe (§6 Ngã rẽ 3)
  - POST_ACTION — chờ kết quả sau submit (§6 Gatekeeper)

· Quy tắc: Quyết định delay PHẢI dựa trên BehaviorState hiện tại.

---

§8.3. CRITICAL_SECTION AWARENESS (↔ Spec §10.3)

· Behavior layer KHÔNG can thiệp CRITICAL_SECTION:

  Các điểm CRITICAL_SECTION (zero delay):
  - Payment submit — click "Complete Purchase" (§5, §6)
  - VBV/3DS handling — iframe interaction + chờ loading (§6 Ngã rẽ 3)
  - API wait — CDP Network.responseReceived pending (§5 Watchdog)
  - Page reload operations (§6 Ngã rẽ 3, 4)

· Quy tắc: Nếu đang trong CRITICAL_SECTION → KHÔNG inject delay.

---

§8.4. SAFE POINT / SAFE ZONE RULE (↔ Spec §10.4)

· Nguyên tắc: Wrapper chỉ thêm delay tại các điểm an toàn (SAFE ZONE). Logic execution không bị thay đổi. Kết quả success/failure không bị ảnh hưởng.

· Delay CHỈ được phép tại (SAFE ZONE):
  - UI interaction (typing, click, hover)
  - Non-critical steps (form navigation, field focus)

· Delay KHÔNG được phép tại:
  - Execution control (scaling, lifecycle transitions)
  - System coordination (runtime loop, watchdog checks)

· Stagger start (§1: random.uniform(12, 25)s) là cơ chế RIÊNG BIỆT:
  - Stagger hoạt động giữa các worker launches
  - Behavior delay hoạt động trong cycle
  - Hai cơ chế KHÔNG can thiệp lẫn nhau

---

§8.5. VÙNG CẤM DELAY — NO-DELAY ZONE (↔ Spec §10.5)

· Behavior layer KHÔNG được inject delay vào:
  - Payment submit (Complete Purchase click event)
  - Watchdog timeout checks
  - Network wait (CDP Network.responseReceived)
  - VBV iframe load/interaction
  - Page reload operations

· Behavior layer KHÔNG phá watchdog:
  - Tổng delay mỗi bước ≤ 7.0s, watchdog timeout = 10s → headroom ≥ 3s
  - Delay bị clamp cứng trước khi áp dụng

· VBV 8–12s wait (§6 Ngã rẽ 3) là OPERATIONAL wait:
  - Chờ iframe loading — không phải behavioral delay
  - KHÔNG được thay thế hoặc bổ sung bởi behavior layer

---

§8.6. KIỂM SOÁT HIỆU NĂNG & MÔ HÌNH XÁC ĐỊNH — ACTION-AWARE DELAY (↔ Spec §10.6)

· Hard constraints (ràng buộc cứng):

  - max_delay_per_action ≤ 1.8s (typing mỗi nhóm 4 số — §4)
  - max_delay_per_hesitation ≤ 5.0s (thinking — §5)
  - total_behavioral_delay_per_step ≤ 7.0s (để lại ≥3s headroom cho watchdog 10s — §5)
  - typing và thinking loại trừ lẫn nhau trong cùng một bước cycle

· Delay phải:
  - Bị clamp (giới hạn) trước khi áp dụng — không bao giờ vượt quá max
  - Không block worker loop — delay thực hiện bằng sleep không chặn luồng chính
  - Không ảnh hưởng watchdog timeout hoặc system-level deadlines

· Overhead trung bình: ≤ 15% so với thời gian cycle không có behavior.

· Hệ thống random:

  rnd = random.Random(seed)

  Trong đó seed là Seed Hành Vi được cấp tại §2.

· Đảm bảo:
  - Reproducible: cùng seed → cùng pattern hành vi (tốc độ gõ, typo, hesitation)
  - Testable: có thể kiểm thử với seed cố định
  - Isolated: mỗi worker có instance random.Random riêng, không chia sẻ state

· Áp dụng cho:
  - Tốc độ gõ phím (typing_speed distribution)
  - Tỷ lệ gõ sai (typo trigger)
  - Thời gian ngập ngừng (hesitation duration)
  - Offset click (Bounding Box ± random)

---

§8.7. QUY TẮC KHÔNG CAN THIỆP — NON-INTERFERENCE RULE (↔ Spec §10.7)

· Behavior layer KHÔNG thay đổi outcome:
  - FSM flow giữ nguyên 100% (4 ngã rẽ — §6)
  - Thứ tự bước execution không đổi
  - Kết quả success/failure không bị ảnh hưởng bởi delay
  - State transitions không bị behavior can thiệp

---

§8.8. ĐỒNG BỘ VỚI PHASE 9 — PHASE 9 ALIGNMENT (↔ Spec §10.8)

· Phase 10 PHẢI tuân thủ:
  - SAFE_POINT — behavior chỉ hoạt động trong ranh giới an toàn (§8.4)
  - CRITICAL_SECTION — zero can thiệp trong các thao tác quan trọng (§8.3)

· Phase 10 KHÔNG ĐƯỢC hoạt động ngoài phạm vi cho phép.

---

9. ANTI-DETECT LAYER 2 TẦNG

TẦNG 1 — ENVIRONMENT & INTERACTION (ĐÃ CÓ TRONG §1-§7):

  · Proxy tĩnh SOCKS5/HTTP map 1-1 với Profile BitBrowser (§1)
  · BitBrowser fingerprint — vân tay trình duyệt duy nhất mỗi cycle (§2)
  · CDP input — toàn bộ thao tác qua Chrome DevTools Protocol, isTrusted=True 100% (§1)
  · Ghost cursor — đường cong Bézier tự nhiên cho di chuyển chuột (§3)
  · Bounding Box Click — offset ngẫu nhiên (x±15, y±5) cho mỗi worker (§4)

TẦNG 2 — BEHAVIORAL BIOMETRICS (BỔ SUNG — Phase 10):

  · Temporal noise (nhiễu thời gian):
    - Phân bố log-normal hoặc gaussian cho inter-keystroke delay
    - Mỗi worker có distribution riêng dựa trên PersonaProfile seed

  · Burst typing (nhịp gõ không đều):
    - Mô phỏng người gõ nhanh rồi dừng, gõ nhanh rồi dừng
    - Kết hợp với quy tắc 4x4: gõ 4 số → khựng → gõ 4 số (§4)

  · Hesitation (ngập ngừng):
    - 3–5s hover/scroll quanh nút trước khi click (§5)
    - Thể hiện hành vi "kiểm tra lại thông tin"

  · Trạng thái tâm lý (fatigue/stress — implicit):
    - Seed persona_type quyết định mức độ ngập ngừng
    - Người già: delay cao hơn, hesitation dài hơn
    - Người trẻ: gõ nhanh hơn, ít ngập ngừng

· QUY TẮC:
  - Tầng 2 KHÔNG phá Tầng 1 — hành vi temporal bổ sung lên environment, không thay thế
  - Tầng 2 KHÔNG thay đổi execution outcome — cùng input → cùng kết quả logic

---

10. MÔ PHỎNG HÀNH VI NGÀY/ĐÊM (DAY/NIGHT BEHAVIOR SIMULATION)

Bổ sung mô phỏng chu kỳ sinh học theo thời gian — tăng cường anti-detect bằng temporal realism.

· Biological Time State:

  Hệ thống xác định trạng thái thời gian dựa trên giờ local (UTC offset theo proxy timezone):

  - DAY (06:00–21:59): trạng thái tỉnh táo, hoạt động bình thường
  - NIGHT (22:00–05:59): trạng thái mệt mỏi, hoạt động chậm hơn

  Xác định: dựa trên proxy IP → timezone (MaxMind GeoLite2, đã có tại §5) → giờ local.

· Behavior Differentiation:

  DAY mode:
  - Tốc độ gõ: ổn định, phù hợp với persona_type (§8 PersonaProfile)
  - Hesitation: ngắn (theo seed — §8.6), ít ngập ngừng
  - Typo rate: baseline theo seed (2–5% — §4)
  - Inter-action delay: đều, biến thiên thấp

  NIGHT mode:
  - Tốc độ gõ: chậm hơn DAY 15–30% (scale factor từ rnd — §8.6)
  - Hesitation: tăng 20–40% so với DAY (fatigue simulation)
  - Typo rate: tăng 1–2% tuyệt đối so với DAY baseline (random trong dải [1%, 2%])
  - Inter-action delay: không đều hơn, variance cao hơn (mô phỏng buồn ngủ)

  Tất cả giá trị vẫn BỊ CLAMP bởi hard constraints §8.6:
  - max_delay_per_action ≤ 1.8s
  - max_delay_per_hesitation ≤ 5.0s
  - total_behavioral_delay_per_step ≤ 7.0s

· Temporal Variation (Biến Thiên Thời Gian):

  Hành vi KHÔNG cố định mà biến động theo thời gian trong cycle:
  - Gradual drift: hành vi thay đổi từ từ qua các bước cycle (không nhảy đột ngột)
  - Micro-variation: mỗi thao tác có nhiễu nhỏ (±5–10%) từ rnd (§8.6)
  - Session fatigue: sau 3+ cycles liên tiếp, hesitation tăng nhẹ (mô phỏng mệt mỏi tích lũy)

  Tất cả variation được tạo từ rnd = random.Random(seed) → reproducible + testable.

· Integration với PersonaProfile (§8):

  PersonaProfile mở rộng thêm thuộc tính temporal:
  - active_hours: khung giờ hoạt động ưa thích (từ seed, ví dụ: persona "trẻ" → 10:00–02:00 next day, wrap-around qua midnight)
  - fatigue_threshold: ngưỡng mệt mỏi (số cycles trước khi session fatigue kích hoạt)
  - night_penalty_factor: hệ số chậm đêm (0.15–0.30, từ seed)

  Vòng đời: Cố định suốt worker lifetime (không đổi giữa các cycles).

· Anti-Detect Enhancement:

  Day/Night model tăng cường Tầng 2 (§9 Behavioral Biometrics):
  - Temporal fingerprint đa dạng: cùng persona nhưng hành vi khác nhau theo giờ
  - Phá pattern đồng nhất: workers chạy cùng lúc nhưng có penalty factor khác nhau
  - Non-periodic: kết hợp DAY/NIGHT + burst typing + hesitation → không có pattern l��p
  - Realistic variance: mô phỏng người thật — ban ngày nhanh, ban đêm chậm và hay nhầm

· Quy tắc an toàn:

  - Day/Night model KHÔNG can thiệp CRITICAL_SECTION (§8.3) — zero delay tại payment submit, VBV, API wait
  - Day/Night model KHÔNG phá watchdog — tất cả delay vẫn bị clamp bởi §8.6 hard constraints
  - Day/Night model KHÔNG ảnh hưởng FSM — flow §6 giữ nguyên 100%, chỉ timing thay đổi
  - Day/Night model KHÔNG thay đổi outcome — kết quả success/failure không phụ thuộc thời gian ngày/đêm

---

11. ĐỒNG BỘ SPEC ↔ BLUEPRINT (SYNCHRONIZATION MATRIX)

Ma trận đối chiếu giữa Spec Phase 10 và Blueprint (1-to-1 structural alignment):

· Spec §10.1 (Architecture) ↔ Blueprint §8.1 (Tích Hợp Thực Thi — Architecture):
  - Spec: wrapper ONLY tại worker execution layer
  - Blueprint: task_fn = wrap(task_fn, persona) tại worker function
  - Status: ✓ ĐỒNG BỘ

· Spec §10.2 (FSM Context) ↔ Blueprint §8.2 (FSM Context — BehaviorState):
  - Spec: IDLE, FILLING_FORM, PAYMENT, VBV, POST_ACTION
  - Blueprint: IDLE, FILLING_FORM, PAYMENT, VBV, POST_ACTION
  - Status: ✓ ĐỒNG BỘ

· Spec §10.3 (CRITICAL_SECTION) ↔ Blueprint §8.3 (CRITICAL_SECTION Awareness):
  - Spec: Payment submit, VBV/3DS, API wait → NO delay
  - Blueprint: Payment submit, VBV/3DS, API wait, Page reload → zero delay
  - Status: ✓ ĐỒNG BỘ (Blueprint mở rộng thêm Page reload — an toàn hơn)

· Spec §10.4 (SAFE POINT/SAFE ZONE) ↔ Blueprint §8.4 (SAFE POINT / SAFE ZONE Rule):
  - Spec: delay chỉ tại UI interaction, non-critical steps
  - Blueprint: wrapper chỉ thêm delay tại SAFE ZONE, stagger tách biệt
  - Status: ✓ ĐỒNG BỘ

· Spec §10.5 (NO-DELAY Zone) ↔ Blueprint §8.5 (Vùng Cấm Delay — NO-DELAY Zone):
  - Spec: Payment submit, Watchdog, Network wait, VBV iframe, Page reload
  - Blueprint: Payment submit, Watchdog, Network wait, VBV iframe, Page reload
  - Status: ✓ ĐỒNG BỘ

· Spec §10.6 (Action-Aware Delay) ↔ Blueprint §8.6 (Kiểm Soát Hiệu Năng & Mô Hình Xác Định):
  - Spec: typing max 1.8s/group, thinking max 5s, total ≤7.0s/step, ≥3s headroom
  - Blueprint: max_delay_per_action ≤ 1.8s, max_delay_per_hesitation ≤ 5.0s, total ≤ 7.0s, ≥3s headroom
  - Spec: Seed-based random, reproducible execution
  - Blueprint: rnd = random.Random(seed), reproducible + testable + isolated
  - Status: ✓ ĐỒNG BỘ

· Spec §10.7 (Non-Interference) ↔ Blueprint §8.7 (Quy Tắc Không Can Thiệp):
  - Spec: no CRITICAL_SECTION delay, no FSM disruption, no side-effects, no order change, no outcome change
  - Blueprint: FSM giữ nguyên, thứ tự không đổi, outcome không ảnh hưởng, state transitions không bị can thiệp
  - Status: ✓ ĐỒNG BỘ

· Spec §10.8 (Phase 9 Alignment) ↔ Blueprint §8.8 (Đồng Bộ Với Phase 9):
  - Spec: respect SAFE_POINT, respect CRITICAL_SECTION
  - Blueprint: SAFE_POINT (§8.4), CRITICAL_SECTION (§8.3), phạm vi cho phép
  - Status: ✓ ĐỒNG BỘ

· Kết luận: Zero mismatch. Blueprint §8.1–§8.8 khớp chính xác 1-to-1 với Spec §10.1–§10.8. Cấu trúc đồng bộ hoàn toàn, sẵn sàng cho audit.

---

12. BILLING SELECTION AUDIT EVENT (SPEC-SYNC §12)

Mỗi lần billing profile được chọn thành công trong `run_payment_step()`, hệ thống phát ra một structured audit event để phục vụ observability và operational tracing.

Trigger: Mỗi lần `billing.select_profile()` trả về thành công (không raise exception) tại `run_payment_step()` trong `integration/orchestrator.py`.

Event Schema:
· event_type: "billing_selection" (literal string)
· worker_id: str — ID của worker thực hiện selection
· task_id: str | None — task_id từ WorkerTask (có thể None nếu task không có task_id)
· selection_method: "zip_match" | "round_robin" — phương thức chọn profile
· "zip_match" nếu zip_code được cung cấp (không phải None/empty)
· "round_robin" nếu zip_code là None hoặc rỗng
· requested_zip: str | None — zip_code được request (raw value từ proxy/request input; None nếu không có)
· profile_id: str — anonymized profile identifier, được tạo từ SHA-256 hash của
  "{first_name}|{last_name}|{profile.zip_code}" (không lộ raw PII)
· trace_id: str — trace_id từ runtime (via `_get_trace_id()`)
· timestamp_utc: str — ISO 8601 UTC timestamp tại thời điểm selection

Privacy Rules:
· KHÔNG log raw first_name, last_name, address, phone, email của billing profile
· profile_id là SHA-256 hash một chiều của "{first_name}|{last_name}|{profile.zip_code}"
· profile.zip_code trong profile_id input chỉ dùng để tạo hash, không xuất hiện trong log riêng
· requested_zip (zip từ proxy) được log để tracing nhưng không kết hợp với tên

Log format: Python `logging` tại level INFO, logger name `integration.orchestrator.audit`
Format: `_AUDIT_LOGGER.info("billing_selection %s", json.dumps(event, ensure_ascii=False))`

Non-interference:
· Audit event KHÔNG ảnh hưởng đến kết quả selection — profile đã được select trước khi emit event
· Nếu event emission gặp exception, log warning và tiếp tục bình thường (không raise)
· Không thêm delay, không thay đổi FSM flow

Synchronization Matrix (§11) — thêm entry mới:
· Spec §12 (Billing Audit Event) ↔ Blueprint §12 (Billing Selection Audit Event):
· Status: ✓ ĐỒNG BỘ

---

13. RUNTIME LIFECYCLE & CONTROL-PLANE SAFETY (PR 13)

Các đảm bảo kỹ thuật bổ sung tại integration/runtime.py để đảm bảo vòng đời worker và control-plane hoạt động đúng trong môi trường concurrent.

§13.1. STOP_WORKER RACE SAFETY
· stop_worker() đọc worker state và thêm vào _stop_requests trong cùng một lock section → không có TOCTOU.
· Worker trong CRITICAL_SECTION không bị buộc dừng — được chờ hoàn thành CS tự nhiên qua join().
· Worker không bị xóa khỏi registry khi timeout — giữ nguyên để thread còn chạy có thể gọi set_worker_state().

§13.2. GRACEFUL SHUTDOWN TIMEOUT BUDGETING
· stop() phân bổ 30% budget cho loop thread, 70% cho workers.
· Stragglers được log rõ ràng với action "hard_timeout" thay vì bị bỏ qua.
· Trạng thái STOPPED được set sau khi tất cả joins hoàn tất (hoặc timeout).
· _flush_idempotency_store() được gọi sau khi state = "STOPPED" → luôn có cơ hội thực thi.

§13.3. RESET() PRODUCTION GUARD
· reset() kiểm tra _state == "RUNNING" và _behavior_delay_enabled == True.
· Nếu đang chạy ở production mode → raise RuntimeError với message rõ ràng.
· Trong test context (_behavior_delay_enabled = False), reset() hoạt động bình thường.

§13.4. _PENDING_RESTARTS CAP
· Khi worker fail, _pending_restarts bị cap tại max(1, len(_workers)).
· Ngăn _pending_restarts tích lũy vô hạn khi nhiều failures xảy ra trước _apply_scale().
· _apply_scale() vẫn decrement đúng số lượng khi scale-up.

§13.5. METRICS UNAVAILABLE DEGRADED PATH
· Khi monitor.get_metrics() fail, log event action = "metrics_unavailable_scaling_deferred".
· Logger WARNING message rõ ràng: "Metrics unavailable; scaling decision deferred for this tick".
· Phân biệt với "hold_deferred" (unsafe worker state) và "hold" (normal HOLD decision).

§13.6. LOG_SINK ERROR COUNTER
· _log_event() bọc log_sink.emit() trong try/except.
· Mỗi lần emit() fail → _log_sink_error_count tăng lên 1.
· Logger WARNING với total failure count → runtime có tín hiệu quan sát được.
· Runtime không crash khi sink fail.

§13.7. START_WORKER PROXY CLEANUP ON THREAD FAILURE
· Nếu Thread.start() raise RuntimeError/OSError → get_default_pool().release(wid) được gọi ngay.
· Proxy không bị leak khi thread không thể khởi động.

§13.8. REGISTER_SIGNAL_HANDLERS NON-MAIN THREAD
· Nếu gọi từ non-main thread → SIGTERM/SIGINT handlers không được register (Python limitation).
· Thêm _logger.debug() rõ ràng để tracing.
· atexit hook vẫn được register từ mọi thread.

Synchronization Matrix (§11) — thêm entries mới:
· Spec §13 (Runtime Lifecycle Safety) ↔ Blueprint §13: Status: ✓ ĐỒNG BỘ
