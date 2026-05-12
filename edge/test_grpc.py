import asyncio
import grpc
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc

async def test_connection():
    try:
        channel = grpc.aio.insecure_channel("localhost:60015")
        stub = voice_pb2_grpc.VoiceServiceStub(channel)
        
        async def dummy_gen():
            yield voice_pb2.ClientMessage(audio_chunk=b"test")
            
        response_iter = stub.Session(dummy_gen())
        
        print("gRPC connection successful!")
        await channel.close()
        return True
    except Exception as e:
        print(f"Connection failed: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_connection())
