# Go - Sending Emails

> Source: https://pocketbase.io/docs/go-sending-emails/

---

Sending emails

PocketBase provides a simple abstraction for sending emails via the `app.NewMailClient()` factory.

Depending on your configured mail settings (_Dashboard > Settings > Mail settings_) it will use the `sendmail` command or a SMTP client.

### [Send custom email](#send-custom-email)

You can send your own custom email from anywhere within the app (hooks, middlewares, routes, etc.) by using `app.NewMailClient().Send(message)`. Here is an example of sending a custom email after user registration:

`// main.go package main import ( "log" "net/mail" "github.com/pocketbase/pocketbase" "github.com/pocketbase/pocketbase/core" "github.com/pocketbase/pocketbase/tools/mailer" ) func main() { app := pocketbase.New() app.OnRecordCreateRequest("users").BindFunc(func(e *core.RecordRequestEvent) error { if err := e.Next(); err != nil { return err } message := &mailer.Message{ From: mail.Address{ Address: e.App.Settings().Meta.SenderAddress, Name: e.App.Settings().Meta.SenderName, }, To: []mail.Address{{Address: e.Record.Email()}}, Subject: "YOUR_SUBJECT...", HTML: "YOUR_HTML_BODY...", // bcc, cc, attachments and custom headers are also supported... } return e.App.NewMailClient().Send(message) }) if err := app.Start(); err != nil { log.Fatal(err) } }`

### [Overwrite system emails](#overwrite-system-emails)

If you want to overwrite the default system emails for forgotten password, verification, etc., you can adjust the default templates available from the _Dashboard > Collections > Edit collection > Options_ .

Alternatively, you can also apply individual changes by binding to one of the [mailer hooks](/docs/go-event-hooks/#mailer-hooks). Here is an example of appending a Record field value to the subject using the `OnMailerRecordPasswordResetSend` hook:

`// main.go package main import ( "log" "github.com/pocketbase/pocketbase" "github.com/pocketbase/pocketbase/core" ) func main() { app := pocketbase.New() app.OnMailerRecordPasswordResetSend("users").BindFunc(func(e *core.MailerRecordEvent) error { // modify the subject e.Message.Subject += (" " + e.Record.GetString("name")) return e.Next() }) if err := app.Start(); err != nil { log.Fatal(err) } }`