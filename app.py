import yagmail
from docxtpl import DocxTemplate
import re
import datetime
import os
import google.generativeai as genai
import json
from flask import Flask, request, Response
import requests

# --- SETTINGS ARE NOW LOADED FROM THE SERVER'S ENVIRONMENT ---
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASS = os.environ.get("GMAIL_PASS")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN") # Added verify token
# -----------------------------------------------------------

TEMPLATE_FILE = "Template.docx"
app = Flask(__name__) 

# --- CONFIGURE GEMINI API (Done once on start) ---
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"!!! CRITICAL: Could not configure Gemini API: {e}")
else:
    print("!!! CRITICAL: GEMINI_API_KEY not found in environment.")

# --- WHATSAPP REPLY FUNCTION ---
def send_whatsapp_reply(to_phone_number, message_text):
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("!!! ERROR: Meta API keys (TOKEN or ID) are missing. Cannot send reply.")
        return

    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = { "messaging_product": "whatsapp", "to": to_phone_number, "type": "text", "text": { "body": message_text } }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Successfully sent WhatsApp reply to {to_phone_number}")
    except requests.exceptions.RequestException as e:
        print(f"!!! ERROR sending WhatsApp reply: {e}")
        if response is not None:
            print(f"Response: {response.text}")

# --- (Other helper functions: parse_command_with_ai, create_quotation, send_email...) ---
# (All the other functions are the same as the last code I sent)
def parse_command_with_ai(command_text):
    print("Sending command to Google AI (Gemini) for parsing...")
    try:
        model = genai.GenerativeModel('models/gemini-pro-latest') 
        system_prompt = f"""
        You are an assistant for a stainless steel trader. Your job is to extract
        quotation details from a user's command.
        The current date is: {datetime.date.today().strftime('%B %d, %Y')}
        Extract the following fields:
        - q_no: The quotation number.
        - date: The date for the quote. If not mentioned, use today's date.
        - company_name: The customer's company name (e.g., "Raj Pvt Ltd").
        - customer_name: The contact person's name (e.g., "Raju").
        - product: The full product description (e.g., "3 inch SS Pipe Sch 40").
        - quantity: The number of items (e.g., "500").
        - rate: The price per item (e.g., "600").
        - units: The unit of measurement (e.g., "Pcs", "Nos", "Kgs"). Default to "Nos" if not specified.
        - hsn: The HSN code (e.g., "7304").
        - email: The customer's email address.
        Return the result ONLY as a single, minified JSON string. Do not add any
        other text, greetings, code blocks (like ```json), or explanations.
        """
        
        full_prompt = system_prompt + "\n\nUser: " + command_text
        response = model.generate_content(full_prompt)
        ai_response_json = response.text.strip().replace("```json", "").replace("```", "").strip()
        print(f"AI response received: {ai_response_json}")
        
        context = json.loads(ai_response_json)
        
        required_fields = ['product', 'customer_name', 'email', 'rate', 'quantity']
        for field in required_fields:
            if field not in context or not context[field]:
                print(f"!!! ERROR: AI did not find a required field: '{field}'")
                return None
        
        try:
            price_num = float(context['rate'])
            qty_num = int(context['quantity'])
            total_num = price_num * qty_num
            
            context['rate_formatted'] = f"₹{price_num:,.2f}"
            context['total'] = f"₹{total_num:,.2f}"
            context['rate'] = context['rate_formatted']
            context['quantity'] = str(qty_num)
        except ValueError:
            print(f"!!! ERROR: AI returned 'rate' or 'quantity' as invalid numbers.")
            return None
            
        if 'date' not in context: context['date'] = datetime.date.today().strftime("%B %d, %Y")
        if 'company_name' not in context: context['company_name'] = ""
        if 'hsn' not in context: context['hsn'] = ""
        if 'q_no' not in context: context['q_no'] = ""
        if 'units' not in context: context['units'] = "Nos"
        
        print(f"Parsed context: {context}")
        return context
        
    except Exception as e:
        print(f"!!! ERROR communicating with Google AI: {e}")
        return None

def create_quotation_from_template(context):
    try:
        doc = DocxTemplate(TEMPLATE_FILE)
    except Exception as e:
        print(f"!!! ERROR: Could not load template '{TEMPLATE_FILE}'.")
        return None
    
    doc.render(context)
    safe_customer_name = "".join(c for c in context['customer_name'] if c.isalnum() or c in " _-").rstrip()
    filename = f"Quotation_{safe_customer_name}_{datetime.date.today()}.docx"
    doc.save(filename)
    print(f"Successfully created '{filename}'")
    return filename

def send_email_with_attachment(recipient_email, subject, body, attachment_path):
    if not attachment_path:
        print("Cannot send email, no attachment was created.")
        return False
    
    if not GMAIL_USER or not GMAIL_PASS:
        print("!!! ERROR: GMAIL_USER or GMAIL_PASS not set. Cannot send email.")
        return False

    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_PASS)
        yag.send(
            to=recipient_email,
            subject=subject,
            contents=body,
            attachments=attachment_path,
        )
        print(f"Email successfully sent to {recipient_email}")
        os.remove(attachment_path)
        print(f"Cleaned up '{attachment_path}'")
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

# --- !! THIS IS THE MAIN FIX !! ---
# This function now accepts GET (for verification) and POST (for messages)
@app.route("/webhook", methods=['GET', 'POST'])
def handle_webhook():
    
    # --- Handle GET request (Meta's verification) ---
    if request.method == 'GET':
        print("Webhook received GET verification request...")
        
        # This is the test Meta sends
        if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token'):
            
            # Check if the token from Meta matches our secret token in Render
            if request.args.get('hub.verify_token') == META_VERIFY_TOKEN:
                print("Verification successful!")
                return Response(request.args.get('hub.challenge'), status=200)
            else:
                print("Verification failed: Token mismatch.")
                return Response("Verification token mismatch", status=403)
        else:
            print("Failed: Did not receive correct hub.mode or hub.verify_token")
            return Response("Failed verification", status=400)

    # --- Handle POST request (A real WhatsApp message) ---
    if request.method == 'POST':
        print("Webhook received POST (new message)!")
        
        customer_phone_number = None
        command_text = None
        
        try:
            data = request.json
            message_data = data['entry'][0]['changes'][0]['value']['messages'][0]
            customer_phone_number = message_data['from']
            command_text = message_data['text']['body']
            
        except Exception as e:
            print(f"Error parsing incoming JSON from Meta: {e}")
            print(f"Full data received: {request.data}") 
            return Response(status=200)
        
        print(f"Received command from {customer_phone_number}: {command_text}")
        
        context = parse_command_with_ai(command_text)
        
        if not context:
            print("Sorry, I couldn't understand that. (AI parsing failed)")
            send_whatsapp_reply(customer_phone_number, "Sorry, I couldn't understand your request. Please check the details and try again.")
            return Response(status=200) 

        print(f"\nGenerating quote for {context['customer_name']}...")
        doc_file = create_quotation_from_template(context)
        
        if not doc_file:
            print("Error: Could not create the document.")
            send_whatsapp_reply(customer_phone_number, "Sorry, an internal error occurred while creating your document.")
            return Response(status=200)

        email_subject = f"Quotation from NIVEE METAL PRODUCTS PVT LTD (Ref: {context.get('q_no', 'N/A')})"
        email_body = f"""
        Dear {context['customer_name']},
        
        Thank you for your enquiry.
        
        Please find our official quotation attached...
        (Your full email text)
        
        Thank you,
        
        Harsh Bhandari
        Nivee Metal Products Pvt. Ltd.
        """
        
        email_sent = send_email_with_attachment(context['email'], email_subject, email_body, doc_file)
        
        if email_sent:
            print("Process complete!")
            reply_msg = f"Success! Your quotation for {context['product']} has been generated and sent to {context['email']}."
            send_whatsapp_reply(customer_phone_number, reply_msg)
        else:
            print("Process failed.")
            send_whatsapp_reply(customer_phone_number, f"Sorry, I created the quote but failed to send the email to {context['email']}.")

        return Response(status=200)

# --- START THE SERVER (FOR PRODUCTION) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)