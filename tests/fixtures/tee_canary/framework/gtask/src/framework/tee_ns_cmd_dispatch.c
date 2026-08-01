/*
 * Copyright (C) 2022 Huawei Technologies Co., Ltd.
 * Licensed under the Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *     http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY OR FIT FOR A PARTICULAR
 * PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include "tee_ns_cmd_dispatch.h"
#include "tee_common.h"
#include "global_task.h"
#include "gtask_inner.h"
#include "agent_manager.h"
#include "session_manager.h"
#include "service_manager.h"
#include "ext_interface.h"
#include "mem_manager.h"
#include "tee_app_load_srv.h"

static TEE_Result process_async_cmd(smc_cmd_t *cmd, bool *async, bool *handled)
{
    TEE_Result ret;

    *handled = true;
    switch (cmd->cmd_id) {
    case GLOBAL_CMD_ID_OPEN_SESSION:
    case GLOBAL_CMD_ID_CLOSE_SESSION:
        ret = process_ta_common_cmd(cmd, CMD_TYPE_NS_TO_SECURE, SMCMGR_PID, async, NULL);
        break;
    case GLOBAL_CMD_ID_SET_SERVE_CMD:
        ret = set_service_thread_cmd(cmd, async);
        break;
    case GLOBAL_CMD_ID_LOAD_SECURE_APP:
        ret = process_load_image(cmd, async);
        break;
    default:
        *handled = false;
        ret = TEE_SUCCESS;
        break;
    }
    return ret;
}

static TEE_Result process_cmd_with_audit_event(const smc_cmd_t *cmd)
{
    TEE_Result ret;
    uint32_t cmd_id = cmd->cmd_id;

    switch (cmd_id) {
    case GLOBAL_CMD_ID_REGISTER_NOTIFY_MEMORY:
        /* not support spi, but tzdriver needs to return success */
        return TEE_SUCCESS;
    case GLOBAL_CMD_ID_REGISTER_LOG_MEM:
        ret = map_rdr_mem(cmd);
        break;
    case GLOBAL_CMD_ID_REGISTER_MAILBOX:
        ret = register_mailbox(cmd);
        break;
    default:
        tloge("invalid cmd 0x%x\n", cmd_id);
        ret = TEE_ERROR_INVALID_CMD;
        break;
    }

    return ret;
}

/* put the commonly used cmd in front to improve performance */
static const struct ns_sync_cmd_t g_ns_sync_cmd_table[] = {
    { GLOBAL_CMD_ID_NEED_LOAD_APP,             need_load_app },

    { GLOBAL_CMD_ID_REGISTER_AGENT,            register_agent },
    { GLOBAL_CMD_ID_UNREGISTER_AGENT,          unregister_agent },

    { GLOBAL_CMD_ID_REGISTER_MAILBOX,          process_cmd_with_audit_event },
    { GLOBAL_CMD_ID_REGISTER_NOTIFY_MEMORY,    process_cmd_with_audit_event },
    { GLOBAL_CMD_ID_REGISTER_LOG_MEM,          process_cmd_with_audit_event },

    { GLOBAL_CMD_ID_ADJUST_TIME,               handle_time_adjust },

    { GLOBAL_CMD_ID_DUMP_MEMINFO,              dump_statmeminfo },
#ifdef CONFIG_ENABLE_DUMP_SRV_SESS
    { GLOBAL_CMD_ID_DUMP_SRV_SESS,             dump_service_session_info },
#endif
    { GLOBAL_CMD_ID_LATE_INIT,                 agent_late_init },
    { GLOBAL_CMD_ID_GET_TEE_VERSION,           get_tee_version },
};

static const uint32_t g_ns_sync_cmd_num =
    sizeof(g_ns_sync_cmd_table) / sizeof(g_ns_sync_cmd_table[0]);

static TEE_Result dispatch_ns_global_cmd(smc_cmd_t *cmd, bool *async)
{
    TEE_Result ret;
    uint32_t i;
    bool handled = false;

    ret = process_async_cmd(cmd, async, &handled);
    if (handled)
        return ret;

    for (i = 0; i < g_ns_sync_cmd_num; i++) {
        if ((cmd->cmd_id == g_ns_sync_cmd_table[i].cmd_id) && (g_ns_sync_cmd_table[i].func != NULL))
            return g_ns_sync_cmd_table[i].func(cmd);
    }

    tloge("dispatch ns cmd failed, invalid cmd id 0x%x\n", cmd->cmd_id);
    return TEE_ERROR_INVALID_CMD;
}

TEE_Result dispatch_ns_cmd(smc_cmd_t *cmd)
{
    TEE_Result ret;
    bool async = false;

    if (cmd == NULL)
        return TEE_ERROR_BAD_PARAMETERS;

    if (cmd->cmd_type == CMD_TYPE_GLOBAL) {
        ret = dispatch_ns_global_cmd(cmd, &async);
    } else {
        /* resume app task that pending on message */
        ret = start_ta_task(cmd, CMD_TYPE_NS_TO_SECURE);
        if (ret == TEE_SUCCESS)
            async = true;
    }

    /* In case of error we should send the error right now */
    if (!async) {
        set_tee_return_origin(cmd, TEE_ORIGIN_TEE);
        set_tee_return(cmd, ret);
        ns_cmd_response(cmd);
    }
    return ret;
}
