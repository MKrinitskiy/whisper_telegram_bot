from enum import unique
from typing import Optional, Any
from telegram import Update

import pymongo
import uuid
from datetime import datetime

import config


class Database:
    def __init__(self):
        print("Connecting to MongoDB")
        self.client = pymongo.MongoClient(config.mongodb_uri)
        print("Connected to MongoDB")

        print("Getting database")
        self.db = self.client["whisper_telegram_bot"]
        print("Got database")

        self.user_collection = self.db["user"]
        self.tasks_collection = self.db["tasks"]
        # self.settings_collection = self.db["settings"]

        dbnames = self.client.list_database_names()
        if 'whisper_telegram_bot' in dbnames:
            print("whisper_telegram_bot database is present")
            unique_id = uuid.uuid4().hex
            self.user_collection.insert_one({"_id": unique_id})
            self.user_collection.delete_one({"_id": unique_id})
            self.tasks_collection.insert_one({"task_id": unique_id})
            self.tasks_collection.delete_one({"task_id": unique_id})
            # self.settings_collection.insert_one({"_id": unique_id})
            # self.settings_collection.delete_one({"_id": unique_id})
        else:
            print("whisper_telegram_bot database is not present. Creating one...")
            try:
                self.user_collection.insert_one({})
                self.tasks_collection.insert_one({})
                # self.settings_collection.insert_one({})

                print("Checking if database is present one more time")
                dbnames = self.client.list_database_names()
                if 'whisper_telegram_bot' in dbnames:
                    print("whisper_telegram_bot database is present")
                else:
                    print("whisper_telegram_bot database is not present")
                    raise ValueError("Database not present")
            except Exception as e:
                print(f"Error: {e}")
                raise ValueError("Database not present")


    # def get_settings(self):
    #     return self.db["settings"].find_one({})
    
    # def set_setting(self, key, value):
    #     self.settings_collection.update_one({}, {"$set": {key: value}})


    def check_if_user_exists(self, user_id: int, raise_exception: bool = False):
        if self.user_collection.count_documents({"_id": user_id}) > 0:
            return True
        else:
            if raise_exception:
                raise ValueError(f"User {user_id} does not exist")
            else:
                return False


    def add_new_user(
        self,
        user_id: int,
        chat_id: int,
        username: str = "",
        first_name: str = "",
        last_name: str = "",
    ):
        user_dict = {
            "_id": user_id,
            "chat_id": chat_id,

            "username": username,
            "first_name": first_name,
            "last_name": last_name,

            "last_interaction": datetime.now(),
            "first_seen": datetime.now(),

            "current_dialog_id": None,
            "current_transcription_lang": "ru"
        }

        if not self.check_if_user_exists(user_id):
            self.user_collection.insert_one(user_dict)

       
    def register_new_task(self, task_description_dict: dict, tg_update: Update):
        self.check_if_user_exists(task_description_dict["user_id"], raise_exception=True)

        # add new dialog
        task_description_dict['update'] = tg_update.to_dict()
        self.tasks_collection.insert_one(task_description_dict)

        # update user's current dialog
        self.user_collection.update_one(
            {"_id": task_description_dict["user_id"]},
            {"$set": {"current_task_id": task_description_dict["task_id"]}}
        )

        return True
    


    def update_task(self, task_id: str, update_dict: dict):
        self.tasks_collection.update_one(
            {"task_id": task_id},
            {"$set": update_dict}
        )


    def unreguster_task_by_id(self, task_id: str):
        self.tasks_collection.delete_one({"task_id": task_id})



    def get_user_current_task(self, user_id: int):
        self.check_if_user_exists(user_id, raise_exception=True)
        task_id = self.get_user_attribute(user_id, "current_task_id")

        if task_id is None:
            return None
        

        return self.tasks_collection.find_one({"_id": task_id})
    


    def get_user_task_by_id(self, user_id: int, task_id: str):
        self.check_if_user_exists(user_id, raise_exception=True)
        
        return self.tasks_collection.find_one({"task_id": task_id,
                                               "user_id": user_id})



    def get_user_attribute(self, user_id: int, key: str):
        self.check_if_user_exists(user_id, raise_exception=True)
        user_dict = self.user_collection.find_one({"_id": user_id})

        if key not in user_dict:
            return None

        return user_dict[key]



    def set_user_attribute(self, user_id: int, key: str, value: Any):
        self.check_if_user_exists(user_id, raise_exception=True)
        self.user_collection.update_one({"_id": user_id}, {"$set": {key: value}})



    def set_dialog_messages(self, user_id: int, dialog_messages: list, dialog_id: Optional[str] = None):
        self.check_if_user_exists(user_id, raise_exception=True)

        if dialog_id is None:
            dialog_id = self.get_user_attribute(user_id, "current_dialog_id")

        self.dialog_collection.update_one(
            {"_id": dialog_id, "user_id": user_id},
            {"$set": {"messages": dialog_messages}}
        )
