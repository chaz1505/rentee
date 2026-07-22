
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import os
import requests
import json

app = Flask(__name__)

CORS(app, resources={r"/*":{"origins":["https://www.rentee.asia","https://rentee.bubbleapps.io"]}})

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SEARCH_URL = "https://www.rentee.asia/api/1.1/wf/search_listings"
CONDO_URL = "https://www.rentee.asia/api/1.1/obj/condo"

@app.route("/")
def home():
    return jsonify({"status":"running"})

def build_response_args(user_message, previous_response_id=None):
    args={
        "model":"gpt-5-mini",
        "input":user_message,
        "instructions":"You are Rentee AI, a Kuala Lumpur property assistant. Always remember the previous conversation. Never expose internal listing IDs.",
        "tool_choice":"auto",
        "tools":[{
            "type":"function",
            "name":"search_listings",
            "description":"Search the Rentee property database.",
            "parameters":{
                "type":"object",
                "properties":{
                    "min_beds":{"type":"integer"},
                    "priceRent":{"type":"number"},
                    "priceSale":{"type":"number"},
                    "condoName":{"type":"string"},
                    "transactionType":{"type":"string"}
                },
                "additionalProperties":False
            }
        }]
    }
    if previous_response_id:
        args["previous_response_id"]=previous_response_id
    return args

def search_listings(tool_args):
    r=requests.get(SEARCH_URL,params={"min_beds":tool_args.get("min_beds",0)},timeout=20)
    r.raise_for_status()
    listings=r.json()["response"]["listing"]
    condo_cache={}
    ui=[]
    gpt=[]
    for listing in listings:
        condo_id=listing.get("condo")
        if not condo_id:
            continue
        if condo_id not in condo_cache:
            c=requests.get(f"{CONDO_URL}/{condo_id}",timeout=20)
            c.raise_for_status()
            condo_cache[condo_id]=c.json().get("response",{}).get("name","Unknown Condo")
        name=condo_cache[condo_id]
        ui.append({"listing_id":listing.get("_id"),"condo":name,"beds":listing.get("beds"),"baths":listing.get("baths"),"price_rent":listing.get("priceRent"),"price_sale":listing.get("priceSale"),"transactionType":listing.get("transactionType")})
        gpt.append({"condo":name,"beds":listing.get("beds"),"baths":listing.get("baths"),"price_rent":listing.get("priceRent"),"price_sale":listing.get("priceSale")})
    return ui,gpt

@app.route("/chat",methods=["POST"])
def chat():
    try:
        data=request.get_json(silent=True) or {}
        previous=data.get("previous_response_id")
        if previous in ("","null"):
            previous=None
        response=client.responses.create(**build_response_args(data.get("message","Show me 3 bed condos"),previous))
        tool_call=next((x for x in response.output if x.type=="function_call"),None)
        if tool_call is None:
            return jsonify({"message":response.output_text,"response_id":response.id,"listings":[]})
        tool_args=json.loads(tool_call.arguments)
        ui,gpt=search_listings(tool_args)
        final=client.responses.create(
            model="gpt-5-mini",
            previous_response_id=response.id,
            input=[{
                "type":"function_call_output",
                "call_id":tool_call.call_id,
                "output":json.dumps(gpt)
            }]
        )
        return jsonify({"message":final.output_text,"response_id":final.id,"listings":ui})
    except Exception as e:
        return jsonify({"error":str(e)}),500

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
