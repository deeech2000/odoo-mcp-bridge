# جسر Odoo ↔ Claude (MCP Server)

خادم بسيط يوصل حساب Odoo عندك بـ Claude، بحيث يقدر يقرأ كشوفات حسابات الموردين
ويجهز فواتير/دفعات **بحالة Draft فقط** — أي شي يسويه هذا الخادم ما يتأكد أو
يُنشر أو تتحرك بيه فلوس فعليًا. لازم إنسان يفتح السجل داخل Odoo ويأكده بنفسه.

## الملفات
- `server.py` — الخادم نفسه (MCP tools)
- `odoo_client.py` — الاتصال بـ Odoo عبر XML-RPC
- `requirements.txt` — المكتبات المطلوبة
- `render.yaml` — إعداد جاهز للنشر على Render
- `.env.example` — مثال للمتغيرات المطلوبة (لا تضع فيه أسرار حقيقية)

## خطوات النشر على Render

1. ادفع هذا المجلد لمستودع GitHub (repo) خاص بك.
2. من Render Dashboard: **New > Web Service** واختر الـ repo.
3. Render بيقرأ `render.yaml` تلقائيًا (Blueprint) أو تعبي الحقول يدويًا:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python server.py`
4. تحت **Environment**، أضف هذي المتغيرات (القيم الحقيقية، ما تنسخها بالكود):
   - `ODOO_URL` = `https://centrixplus-caf.odoo.com`
   - `ODOO_DB` = `centrixplus-caf`
   - `ODOO_USERNAME` = بريدك (a.algahtani@cafcafe.com)
   - `ODOO_API_KEY` = المفتاح اللي أخذته من Odoo (Account Security > API Keys)
   - `MCP_SHARED_SECRET` = كلمة سر تخترعها أنت (طويلة وعشوائية) — تحمي الخادم من أي شخص يعرف رابط Render
5. اضغط **Create Web Service**. بعد النشر، Render بيعطيك رابط مثل:
   `https://odoo-mcp-bridge.onrender.com`
6. تأكد الخادم شغال: افتح `https://odoo-mcp-bridge.onrender.com/healthz` — يفترض يرجع `ok`.

## ربطه بـ Claude

1. Settings > Connectors > Add custom connector
2. **Server URL:** `https://odoo-mcp-bridge.onrender.com/mcp`
3. Advanced settings > Headers: أضف هيدر
   - Name: `X-API-Key`
   - Value: نفس قيمة `MCP_SHARED_SECRET` اللي وضعتها بـ Render
4. Add، وبعدها فعّله بالمحادثة من زر "+" > Connectors.
5. أي أداة كتابة (create_draft_vendor_bill / create_draft_vendor_payment) تبقى
   على "Ask each time" — لا تحولها لـ "Always allow".

## ملاحظة أمان مهمة

- لا تشارك `ODOO_API_KEY` أو `MCP_SHARED_SECRET` بأي محادثة أو شات — تدخل
  فقط في إعدادات Render مباشرة.
- كل الأدوات اللي "تكتب" بالنظام تستخدم `create()` فقط — لا تستدعي أي دالة
  تأكيد/نشر/دفع. راجع `server.py` و`odoo_client.py` للتأكد بنفسك.
