import boto3

session = boto3.Session(
    profile_name="CarePlanAdmin-768669379100",
    region_name="us-east-1"
)

bedrock = session.client("bedrock-runtime")

response = bedrock.converse(
    modelId="arn:aws:bedrock:us-east-1:768669379100:inference-profile/us.anthropic.claude-3-sonnet-20240229-v1:0",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "text": "Explain diabetes in simple terms"
                }
            ]
        }
    ],
    inferenceConfig={
        "temperature": 0.1,
        "maxTokens": 500
    }
)

print(response["output"]["message"]["content"][0]["text"])