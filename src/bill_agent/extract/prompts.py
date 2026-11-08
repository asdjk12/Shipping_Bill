"""抽取和审核的 prompt。

分三部分：输入格式说明（让模型看懂解析 / OCR 的输出）、抽取规则（test.pdf 四.2、七 的要求）、
审核要求。审核沿用同一份抽取规则，两个模型按同一个标准判断。
业务判断（AR/AP、匹配、标准化）不在这里做，由确定性代码完成。
"""

INPUT_FORMAT = """# 输入格式
- 每个文件以 "===== 文档 N：<来源路径>（<类型等>）=====" 开头。同一组文件来自同一个上传文件，
  例如一封邮件的正文和它的附件。
- PDF 文本保留了原排版：列靠空格对齐；表格式排版中标签和值可能分在上下两行。
- OCR 识别的表格单元格用 " | " 分隔；"--- page N ---" 是分页标记；
  "[未归入版面块的识别文本]" 下面是版面分析遗漏的文字，内容同样有效。
- "[识别提示]" 列出识别置信度低、可能有误的文本。
"""

EXTRACTION_RULES = """# 抽取规则
1. 每张收费单据输出一个 document（Debit Note、Credit Note、Invoice、费用通知等）。
   邮件正文里写明金额的收费也要输出一个 document（如 "A pay B：H/C $50"），
   没有的单据信息（类型、编号、日期）填 null。
   提单、到货预报等不含收费的文件不单独输出。source_doc 填该收费所在文件的编号 N。
2. 只抽取本单实际收取的费用行。条款或说明中的费率（如 "每小时 $150"、"取消收 $25"）不是费用；
   明确写明免收的费用（如 "Carrier Free H/C"）不输出；多页重复的页眉、表头只抽取一次。
3. 一个费用行对应一个 charge；line_no 按单据上的顺序从 1 开始编号；同名费用分别输出。
   charge_name_raw 逐字照抄费用名称这一栏的完整内容，不删减，例如 "OCA17 On Carriage Additional - US Em"
   要全部保留；其他列的内容不算费用名称。
   payment_term_raw 只取明确标注为付款方式的列或文字（P/C、PP/CC、Prepaid/Collect）；
   Base、Unit 等其他列不是付款方式，单据上没有付款方式时填 null。
4. 所有值逐字照抄原文，不翻译、不改写、不换算、不补全：
   - 日期保持原格式；金额保持原文写法（含千分位逗号，如 "1,310.40"），不带币种符号；
   - 币种照抄（如 "USD"、"$"）；付款方式照抄（如 "PP"、"CC"、"C"、"Prepaid"）；
   - 公司名不带末尾括号里的站点简称：原文 "NEPTUNE SHIPPING LIMITED (LAX)" 填 "NEPTUNE SHIPPING LIMITED"。
     名称中间的括号是名称的一部分，要保留，如 "CMA CGM (AMERICA) LLC"。
     公司名也不带后面的代理说明：原文 "X Co., LLC As Agents for Y Ltd." 填 "X Co., LLC"。
5. 通用原则：所有字段都按语义判断，文件内容没有相关语义时一律填 null 或空列表。不推测，不用相近的信息
   代替，也不从编号格式、公司名称等线索推断。费用行没有币种时 currency_raw 为 null；
   单据级 currency_raw 填单据头部或合计行写明的币种（如 "CURRENCY USD"、"TOTAL CHARGES TO BE PAID: USD"、
   合计金额前的 "USD"）；只有费用行写了币种时不要推断。
6. 贷记列（CREDIT）或退款的金额，在原文数字前加负号，如 "-35.00"。
7. issuer 和 recipient 按语义判断，不要只找同名标签。先理解单据上各方的角色，再判断这张单据是谁发给谁的：
   - issuer 是实际收取本单费用的航运公司，判断方式与 carrier 相同；Credit Note 则是退款的一方。
     页面顶部印着的公司不一定是 issuer，要看费用实际由谁收取，例如 REMIT TO、BALANCE DUE TO 指向的公司。
   - recipient 是这张单据发送给的一方，原文可能写作 Bill To、To、Attention、Messrs 等。
   - 到货通知、费用通知这类发给收货人的单据，CONSIGNEE 或 NOTIFY 通常就是 recipient。
   - 页面顶部的公司同时是 CONSIGNEE 或 NOTIFY 时，说明顶部印的是收件信息，它是 recipient。
   - 没有写明接收方时，不要因为某家公司印在页面顶部就把它填为 recipient 或 payer。
   - 判断不出来时填 null，不要猜。
   货物三方单独填写，与账单双方无关：寄件人委托航运公司，把货送给收件人。
   这三个字段按语义判断，不依赖固定标签：读完同组全部文件后，判断这票货是谁寄出的、送给谁、由谁运送。
   - shipper 是寄件人，consignee 是收件人。原文可能写作 SHIPPER、CONSIGNEE、Sender、Receiver、
     Exporter、Importer、From、To、Ship To、Deliver To、Issued To，也可能没有标签，只能从上下文看出。
     值必须是公司或个人。From、To 等写的如果是港口、城市等地点，就不是寄件人或收件人。
   - carrier 是送货人，只看谁收取本单费用：收取本单费用的航运公司就是 carrier。
     船名、船属于哪家公司、提单号前缀都不影响判断。单据上出现多家航运公司时，取收取本单费用的那一家。
     提单上的 SHIPPER、FORWARDING AGENT 不是送货人。文件里没有航运公司收取费用时填 null。
   - 邮件正文、附件提单等同组文件里的信息都可以用。同一票货同时有分提单（HBL）和主提单（MBL）时，
     寄件人和收件人取 HBL 上的，MBL 上的通常是货代之间的关系。
   - 读完全部文件仍判断不出来时填 null，不要猜。经手方、中转方不记录。
   payee / payer 只在原文明确写出收款方、付款方时填写，例如 "BALANCE DUE TO X"、"REMIT TO X"、
   "payments must be issued to X"、"Payer: X"、"A pay B"（A 是付款方，B 是收款方）。
   多栏排版中标签后的值可能在下面几行的同一列里。
8. bl_nos 填所有提单号（主单、分单都填）；container_nos 填所有箱号；booking_no 填订舱号。
   同组其他文件（邮件正文、附件提单）中明确属于同一票货的提单号、箱号，也可以填入。
9. totals 按币种填写单据上写明的小计、税额、总计；单据上没有写的不填，不要自己计算。
10. change_instructions 只在原文明确要求修改已有账单时填写，例如 "请将 Handling Fee 改为 USD 40"。
11. evidence：为每个非空字段给出证据。field 是字段名，quote 是原文中同一行内连续的文字，逐字照抄；
    标签和值不在同一行时，只引用值所在的那一行，不要把不同行的文字拼在一起，也不要用省略号。
    单据的 evidence 只能用单据级字段名（如 reference_no、issuer、payee、shipper、consignee、bl_nos、totals）；
    单据级字段的 quote 必须包含字段值，值跨行时至少引用其中一行，否则该字段会被清空。
    费用行的 evidence 只能用费用行字段名（如 charge_name_raw、amount、currency_raw）。
"""

SYSTEM_PROMPT = ("你是航运财务单据的信息抽取器。从给定文件中抽取收费单据信息，按要求的结构输出。\n\n"
                 + INPUT_FORMAT + "\n" + EXTRACTION_RULES)

HUMAN_PROMPT = "文件内容：\n\n{context}"

RETRY_PROMPT = """上一次的输出没有通过校验：
{errors}

上一次的输出：
{previous}

请修正这些问题，重新输出完整的抽取结果。原文里确实没有的内容填 null，不要编造。"""

PATCH_PROMPT = """上一次的输出没有通过校验，只需要修正下面列出的问题，其他内容保持不变：
{errors}

上一次的输出：
{previous}

只输出需要修正的内容，不要重复输出整份结果：
- fields：单据级字段的问题。每项给出单据序号 document（上一次输出中 documents 的第几项，从 1 开始）、
  字段名 field、对照原文核实后的值 value（bl_nos、container_nos 用 values 列表给出完整取值），
  以及包含该值的原文摘录 quote（同一行内连续的文字）。原文确实没有该信息时 value 填 null。
  totals 的证据有问题时也在 fields 里给出：field 填 totals，只给 quote，value 留空。
- prices：价格的问题。只对照原文复核这张单据的价格：charges 按 line_no 给出每个费用行的
  quantity、unit_price、amount（照抄原文写法）和证据；totals 给出单据上写明的合计，不需要修改时填 null。
  不要增删费用行，不要修改费用名称。"""

REVIEW_SYSTEM_PROMPT = ("""你是航运财务单据抽取结果的审核员。另一个模型已按下面的抽取规则从文件中抽取了结构化信息。
你对照原文判断抽取结果的语义是否正确，只给出判断，不修改结果。

""" + INPUT_FORMAT + "\n" + EXTRACTION_RULES + """
# 不用检查的内容
值能否在原文中找到、金额加总是否正确，代码已经检查过。
数字只看数值，不看写法：5.0 和 5.00、245.0 和 245.0000 相同，不算错误。

# 审核内容
1. 逐个字段判断值的含义是否相符：标为提单号的值在原文中是否是提单号，签发方是否真是签发单据的公司，
   金额、单价、数量是否取自对应的列，付款方式是否取自标注付款方式的列。
2. 值为 null 或空列表的字段，判断原文是否明确写了该信息而被漏掉。
   特别是 payee：要在全文查找 REMIT TO、BALANCE DUE TO、payments must be issued to 这类收款指示。
3. 判断是否有不该抽取的内容，例如把条款里的费率、明确免收的费用当成费用行；
   文件内容没有相关语义却填了值的字段，同样判为错误。
4. 逐行核对原文的收费表和抽取的费用行：原文实际收取、但抽取结果没有的费用行或单据填入 omissions。
   费用行为空而原文有收费金额时，也属于漏抽。

# 输出要求
- verdicts 必须覆盖待审核字段中的每一个 key：document 填单据编号，field 原样填写 key。
- 能从原文指出错误时 ok 为 false，reason 写清错在哪里，quote 逐字照抄支持这一判断的原文，
  必须是同一行内连续的文字。ok 为 true 时 reason 和 quote 留空。
- 同一公司或同一数值有多种写法时，取哪一种都算正确。拿不准时判为 ok。
""")

REVIEW_HUMAN_PROMPT = """文件内容：

{context}

抽取结果（单据编号 -> 待审核字段）：

{extraction}"""

REVIEW_INCOMPLETE_PROMPT = """上一次的审核没有完成：{problem}

请重新输出完整的审核结果，覆盖待审核字段中的每一个 key。"""
