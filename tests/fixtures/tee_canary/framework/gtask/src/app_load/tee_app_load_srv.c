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
#include "tee_app_load_srv.h"
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <openssl/obj_mac.h>
#include <mem_ops.h>
#include <fileio.h>
#include <sys/mman.h>
#include <dyn_conf_dispatch_inf.h>
#include <spawn_ext.h>
#include "tee_mem_mgmt_api.h"
#include "gtask_inner.h"
#include "mem_manager.h"
#include "service_manager.h"
#include "session_manager.h"
#include "tee_log.h"
#include "securec.h"
#include "tee_crypto_api.h"
#include "permsrv_api.h"
#include "dynload.h"
#include "tee_load_lib.h"

#include "drvcall_dyn_conf_builder.h"
#include "drv_dyn_conf_builder.h"
#include "target_type.h"
#include "tee_elf_verify.h"
#include "global_task.h"
#include "task_dynamic_adaptor.h"
#include "load_app_comm.h"
#include <sys/mman.h>

typedef struct {
    smc_cmd_t smc_cmd;
    bool elf_loading;    /* this flag is only for perm service crash callback */
} elf_load_context_t;

static elf_load_context_t g_load_context;

TEE_Result rename_tmp_file(const char *new_name, uint32_t len)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return TEE_ERROR_GENERIC;
    }
    if (len == 0 || len > MAX_TAFS_NAME_LEN || new_name == NULL) {
        tloge("new file name error\n");
        return TEE_ERROR_BAD_PARAMETERS;
    }

    if (vfs_rename(img_info_ptr->img_fp, new_name) < 0) {
        tloge("rename tmp file name failed\n");
        return TEE_ERROR_GENERIC;
    }

    for (uint32_t idx = 0; idx < len; idx++)
        img_info_ptr->tmp_file_name[idx] = new_name[idx];
    img_info_ptr->tmp_file_name[len - 1] = '\0';

    return TEE_SUCCESS;
}

static void unlink_file(void)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return;
    }
    if (img_info_ptr->tmp_file_exist == false)
        return;

    if (unlink(img_info_ptr->tmp_file_name) != TEE_SUCCESS)
        tloge("file unlink failed\n");
    else
        img_info_ptr->tmp_file_exist = false;
}

static TEE_Result close_tmp_file(void)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return TEE_ERROR_GENERIC;
    }
    if (img_info_ptr->img_fp < 0)
        return TEE_SUCCESS;

    if (close(img_info_ptr->img_fp) != 0) {
        tloge("tmp fileclose failed\n");
        return TEE_ERROR_GENERIC;
    }
    img_info_ptr->img_fp = -1;

    return TEE_SUCCESS;
}

static void do_remove_file(void)
{
    if (close_tmp_file() != TEE_SUCCESS)
        tloge("close tmp file failed\n");
    unlink_file();
}

void free_img_load_buf(void)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr->img_buf == NULL)
        return;

    /* do NOT free, map from tafs */
    (void)munmap((void *)(uintptr_t)img_info_ptr->img_buf, img_info_ptr->aligned_img_size);
    img_info_ptr->img_buf = NULL;

    do_remove_file();

    (void)memset_s(img_info_ptr, sizeof(*img_info_ptr), 0, sizeof(*img_info_ptr));
    img_info_ptr->img_fp = -1;

    (void)memset_s(&g_load_context, sizeof(g_load_context), 0, sizeof(g_load_context));
}

TEE_Result need_load_app(const smc_cmd_t *cmd)
{
    TEE_UUID srv_uuid;
    uint32_t param_type = 0;
    TEE_Param *params = NULL;
    int32_t need_load;

    if (cmd == NULL)
        return TEE_ERROR_BAD_PARAMETERS;
    /* get uuid */
    TEE_Result ret = cmd_global_ns_get_params(cmd, &param_type, &params);
    if (ret != TEE_SUCCESS)
        return TEE_ERROR_BAD_PARAMETERS;
    if (params == NULL)
        return TEE_ERROR_BAD_PARAMETERS;
    if (TEE_PARAM_TYPE_GET(param_type, 0) != TEE_PARAM_TYPE_MEMREF_INOUT)
        return TEE_ERROR_BAD_PARAMETERS;
    if (params->memref.size < sizeof(TEE_UUID))
        return TEE_ERROR_BAD_PARAMETERS;
    /* get uuid */
    errno_t eret = memcpy_s(&srv_uuid, sizeof(TEE_UUID), params->memref.buffer, sizeof(TEE_UUID));
    if (eret != EOK)
        return TEE_ERROR_SECURITY;

    if (need_load_srv(&srv_uuid) == false) {
        tlogd("no need load app\n");
        need_load = 0;
    } else {
        tlogd("need load app\n");
        need_load = 1;
    }

    *(int32_t *)(params->memref.buffer) = need_load;

    if (need_load != 0)
        tlogd("need_load flag is %x ==================\n", need_load);

    return TEE_SUCCESS;
}

static TEE_Result tee_cmd_params_parse(const smc_cmd_t *cmd, TEE_Param **params)
{
    TEE_Result tee_ret;
    uint32_t param_type = 0;

    /* get params for cmd, and check the param_type */
    tee_ret = cmd_global_ns_get_params(cmd, &param_type, params);
    if (tee_ret != TEE_SUCCESS) {
        tloge("Failed to map cmd operation");
        return TEE_ERROR_GENERIC;
    }
    bool check = (TEE_PARAM_TYPE_GET(param_type, 0) != TEE_PARAM_TYPE_MEMREF_INPUT ||
                  TEE_PARAM_TYPE_GET(param_type, 1) != TEE_PARAM_TYPE_VALUE_INOUT ||
                  TEE_PARAM_TYPE_GET(param_type, OUTPUT_MEM_REF_INDEX) != TEE_PARAM_TYPE_MEMREF_OUTPUT ||
                  TEE_PARAM_TYPE_GET(param_type, INPUT_VALUE_INDEX) != TEE_PARAM_TYPE_VALUE_INOUT);
    if (check) {
        tloge("Bad expected parameter types");
        return TEE_ERROR_BAD_PARAMETERS;
    }
    if (*params == NULL ||
        (*params)->memref.buffer == NULL ||
        (*params)->memref.size <= sizeof(int32_t) ||
        (*params)[OUTPUT_MEM_REF_INDEX].memref.size != sizeof(TEE_UUID))
        return TEE_ERROR_GENERIC;

    return TEE_SUCCESS;
}

static TEE_Result tee_secure_img_get_version(const uint8_t *share_buf, uint32_t buff_size, uint32_t *img_version)
{
    bool check = (share_buf == NULL || img_version == NULL || buff_size < sizeof(teec_image_identity));
    if (check)
        return TEE_ERROR_BAD_PARAMETERS;
    teec_image_identity img_identity = {0};
    img_identity = *(teec_image_identity *)share_buf;

    /* decide the TA version */
    bool temp_check = (img_identity.magic_num1 == TA_HEAD_MAGIC1) && (img_identity.magic_num2 == TA_HEAD_MAGIC2) &&
                 (img_identity.version_num > 1);
    if (temp_check) {
        tlogd("new version ta, version num 0x%x\n", img_identity.version_num);
        *img_version = img_identity.version_num;
    } else {
        /* Fisrt version of TA doesn't include magic number and version number */
        tlogd("old version ta\n");
        *img_version = TA_SIGN_VERSION;
    }

    return TEE_SUCCESS;
}

static TEE_Result create_empty_file(elf_image_info *img_info_ptr)
{
    static uint32_t file_tmp_number = 0;
    if (snprintf_s(img_info_ptr->tmp_file_name, MAX_TAFS_NAME_LEN, MAX_TAFS_NAME_LEN - 1, LOAD_TA_TMP_FILE,
        TAFS_MOUNTPOINT, file_tmp_number) < 0) {
        tloge("generate tmp file name failed\n");
        return TEE_ERROR_BAD_PARAMETERS;
    }
    /* the value of file_tmp_number has no meaning, just change it to distinguish tmp file  */
    file_tmp_number++;
    img_info_ptr->img_fp = open(img_info_ptr->tmp_file_name, O_CREAT | O_RDWR, RWRIGHT, (uint64_t)0);
    if (img_info_ptr->img_fp < 0) {
        tloge("file open failed: %d\n", img_info_ptr->img_fp);
        return TEE_ERROR_BAD_PARAMETERS;
    }
    img_info_ptr->tmp_file_exist = true;
    if (ftruncate(img_info_ptr->img_fp, img_info_ptr->aligned_img_size) != 0) {
        tloge("file truncate failed\n");
        do_remove_file();
        return TEE_ERROR_BAD_PARAMETERS;
    }

    return TEE_SUCCESS;
}

static TEE_Result get_img_load_buf(uint32_t size)
{
    if (size == 0 || size > PAGE_ALIGN_UP(size + ADDITIONAL_BUF_SIZE)) {
        tloge("invalid img size %u\n", size);
        return TEE_ERROR_BAD_PARAMETERS;
    }

    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return TEE_ERROR_GENERIC;
    }
    img_info_ptr->aligned_img_size = PAGE_ALIGN_UP(size + ADDITIONAL_BUF_SIZE); /* get a redundance */

    if (create_empty_file(img_info_ptr) != TEE_SUCCESS)
        return TEE_ERROR_BAD_PARAMETERS;

    void *map_addr_gtask = vfs_mmap(img_info_ptr->img_fp, img_info_ptr->aligned_img_size, 0);
    if (map_addr_gtask == NULL) {
        tloge("map file from tafs failed\n");
        do_remove_file();
        return TEE_ERROR_BAD_PARAMETERS;
    }

    img_info_ptr->img_size = size;
    img_info_ptr->img_buf = map_addr_gtask;
    img_info_ptr->img_offset = 0;
    return TEE_SUCCESS;
}

static TEE_Result handle_img_alloc_img_buff(uint32_t img_version, uint8_t *share_buf, uint32_t buf_len)
{
    TEE_Result ret;
    uint32_t img_size = 0;

    ret = tee_secure_get_img_size(img_version, share_buf, buf_len, &img_size);
    if (ret != TEE_SUCCESS) {
        tloge("get img size failed, ret=0x%x, img version=%u\n", ret, img_version);
        return ret;
    }

    ret = get_img_load_buf(img_size);
    if (ret != TEE_SUCCESS) {
        tloge("Failed to get buf to load TA, img size %u\n", img_size);
        return ret;
    }
    return TEE_SUCCESS;
}

static TEE_Result tee_secure_image_recieve(uint8_t *share_buf, uint32_t buf_len)
{
    uint32_t img_version = 0;
    errno_t eret;

    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return TEE_ERROR_GENERIC;
    }
    /* The first time of TA image transfer, it may needs several time to complete */
    if (img_info_ptr->img_buf == NULL) {
        /* Get TA image version number */
        TEE_Result ret = tee_secure_img_get_version(share_buf, buf_len, &img_version);
        if (ret != TEE_SUCCESS)
            return ret;
        img_info_ptr->img_version = img_version;

        ret = handle_img_alloc_img_buff(img_version, share_buf, buf_len);
        if (ret != TEE_SUCCESS)
            return ret;
    }

    /* Check the memcpy size */
    if (img_info_ptr->img_offset > img_info_ptr->img_size ||
        buf_len > (img_info_ptr->img_size - img_info_ptr->img_offset))
        return TEE_ERROR_GENERIC;

    eret = memcpy_s(img_info_ptr->img_buf + img_info_ptr->img_offset,
        img_info_ptr->img_size - img_info_ptr->img_offset, share_buf, buf_len);
    if (eret != EOK)
        return TEE_ERROR_SECURITY;

    img_info_ptr->img_offset += buf_len;
    return TEE_SUCCESS;
}

static TEE_Result recv_img_info_from_tzdriver(const smc_cmd_t *cmd, TEE_Param **params)
{
    TEE_Result ret;
    int32_t keep_loading;
    uint8_t *share_buf = NULL;
    uint32_t buf_len;

    if (cmd == NULL || params == NULL)
        return TEE_ERROR_BAD_PARAMETERS;

    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (img_info_ptr == NULL) {
        tloge("the ptr is null\n");
        return TEE_ERROR_GENERIC;
    }

    /* Get params for cmd, and check the param_type */
    ret = tee_cmd_params_parse(cmd, params);
    if (ret != TEE_SUCCESS) {
        tloge("Failed to parse cmd parameters\n");
        return ret;
    }
    /* load elf start */
    if ((*params)[INPUT_VALUE_INDEX].value.a == 0)
        free_img_load_buf();

    /* Get load flag for share buf, load_flag decide whether we need load again */
    share_buf = (*params)->memref.buffer;
    /* First byte of cmd share buffer indicates that if transfer need continue */
    keep_loading = *(int32_t *)share_buf;
    share_buf += sizeof(int32_t);
    buf_len = (*params)->memref.size - sizeof(int32_t);

    /* Receive secure image, it may be sent several times for length limit reason */
    ret = tee_secure_image_recieve(share_buf, buf_len);
    if (ret != TEE_SUCCESS) {
        tloge("Failed to receive or parse ta image header: 0x%x\n", ret);
        return ret;
    }

    if ((img_info_ptr->img_offset < img_info_ptr->img_size) && (keep_loading == 1))
        /* Img send not finished yet, that's why we don't free g_img_info.img_buf here */
        return RET_KEEP_LOADING;

    return TEE_SUCCESS;
}

#ifdef CONFIG_DYNLIB_LOAD_SUPPORT
static TEE_Result load_secure_lib(load_elf_func_params *param, const TEE_UUID *uuid,
                                  tee_img_type_t type)
{
    struct service_struct *service = NULL;
    char file_name[LIB_NAME_MAX] = {0};

    param->fname = file_name;
    param->fname_size = sizeof(file_name);

    // 1. find service
    // 2. add so to service,remember to remove lib when del service
    // 3. for no xip,need to del lib when map lib
    int32_t rc = find_service(uuid, 0, &service);
    if (rc == -1 || service == NULL) {
        tloge("service is not exist.uuid=%x\n", uuid->timeLow);
        return TEE_ERROR_GENERIC;
    }

    rc = (int32_t)dynamic_load_lib_elf(param, service, uuid, 0, type);
    if (rc == LOAD_FAIL) {
        tloge("store lib elf fail\n");
        return TEE_ERROR_GENERIC;
    } else if (rc == LIB_EXIST) {
        return TEE_SUCCESS;
    }

    TEE_Result ret = tee_add_libinfo(service, file_name, (size_t)sizeof(file_name), type);
    if (ret != TEE_SUCCESS) {
        // fail to link lib to service, need to release lib file
        int result = unlink(file_name);
        if (result != 0)
            tloge("unlink %s failed\n", file_name);
        return ret;
    }
    return TEE_SUCCESS;
}

static TEE_Result load_secure_lib_image(tee_img_type_t type, const elf_verify_reply *verify_reply)
{
    TEE_Result ret;
    load_elf_func_params param;
    TEE_UUID uuid = {0};
    TEE_UUID gtask_uuid = TEE_SERVICE_GLOBAL;
    elf_image_info *img_info_ptr = get_img_info_ptr();

    param.file_buffer = (char *)img_info_ptr->ptr_ta_elf;
    param.file_size = verify_reply->payload_hdr.ta_elf_size;
    param.lib_name = (char *)verify_reply->service_name;
    uuid = verify_reply->srv_uuid;

    /* dynamic_drv is in lib_list of gtask, heap_size of dynamic_drv should be large than it's file size */
    if ((type == IMG_TYPE_DYNAMIC_DRV) || (type == IMG_TYPE_CRYPTO_DRV) ||
        (type == IMG_TYPE_DYNAMIC_CLIENT)) {
        if ((uint32_t)verify_reply->ta_property.heap_size < (uint32_t)param.file_size) {
            tloge("error: dynamic_drv heap_size = %u < file_size = %u, file_name = %s\n",
                  (uint32_t)verify_reply->ta_property.heap_size, (uint32_t)param.file_size, param.lib_name);
            return TEE_ERROR_BAD_PARAMETERS;
        }
        uuid = gtask_uuid;
    }

    ret = load_secure_lib(&param, &uuid, type);
    if (ret != TEE_SUCCESS) {
        tloge("Failed to load library image, file_name = %s\n", param.lib_name);
        return ret;
    }
    return TEE_SUCCESS;
}
#else
static TEE_Result load_secure_lib_image(tee_img_type_t type, const elf_verify_reply *verify_reply)
{
    (void)type;
    (void)verify_reply;
    tloge("Unsupported dyn library load\n");
    return TEE_ERROR_NOT_SUPPORTED;
}
#endif


static void tee_unregister_dyn_config(elf_verify_reply *verify_reply)
{
    if (verify_reply->conf_registed)
        tee_ext_notify_unload_ta(&verify_reply->srv_uuid);

    /* if the drv or drvcall has registered dyn perm, we should unregister them */
    if (verify_reply->dyn_conf_registed) {
        if (verify_reply->mani_ext.target_type == DRV_TARGET_TYPE)
            unregister_conf(uninstall_drv_permission, verify_reply->service_name,
                verify_reply->service_name_len);
        else if (verify_reply->mani_ext.target_type == TA_TARGET_TYPE ||
            verify_reply->mani_ext.target_type == SRV_TARGET_TYPE ||
            verify_reply->mani_ext.target_type == CLIENT_TARGET_TYPE)
            unregister_conf(uninstall_drvcall_permission, &verify_reply->srv_uuid, sizeof(struct tee_uuid));
        else
            tloge("type %d is invalid, cannot unregister dyn config\n", verify_reply->mani_ext.target_type);

        verify_reply->dyn_conf_registed = false;
    }
}

static TEE_Result load_secure_file_image_pre(const smc_cmd_t *cmd, TEE_Param **params_back)
{
    TEE_Param *params = NULL;

    /* do age service & lib */
    TEE_Result ret = age_service();
    if (ret != TEE_SUCCESS)
        return ret;

    age_timeout_lib();

    /* recv img & alloc img buf */
    set_load_ta_mode_global_ptr();
    ret = recv_img_info_from_tzdriver(cmd, &params);
    if (ret != TEE_SUCCESS)
        return ret;

    *params_back = params;
    return TEE_SUCCESS;
}

static TEE_Result check_verify_reply(TEE_Param *params,
    elf_verify_reply *verify_reply, tee_img_type_t *img_type)
{
    TEE_Result ret;

    if (verify_reply->off_ta_elf == INVALID_OFFSET) {
        tloge("empty ta elf buffer\n");
        return TEE_ERROR_BAD_PARAMETERS;
    }

    elf_image_info *img_info_ptr = get_img_info_ptr();
    /* check img permission */
    ret = tee_secure_img_permission_check(img_info_ptr->img_version, verify_reply);
    if (ret != TEE_SUCCESS) {
        tloge("Failed to pass img permission check\n");
        return ret;
    }

    /* check img type */
    *img_type = tee_secure_get_img_type(verify_reply, img_info_ptr->img_version);

    /*
     * before load_dyn_drv, we need judge it's type from teecd , dynamic drv or crypto drv
     * or dynamic client
     */
    if ((params != NULL) && params[1].value.a == LOAD_DYNAMIC_DRV && *img_type != IMG_TYPE_DYNAMIC_DRV &&
        *img_type != IMG_TYPE_CRYPTO_DRV && *img_type != IMG_TYPE_DYNAMIC_CLIENT) {
        tloge("load_dyn_drv only support dynamic_drv type, img_type = %d, file_name = %s\n",
              *img_type, (char *)verify_reply->service_name);
        return TEE_ERROR_BAD_PARAMETERS;
    }

    return ret;
}

static TEE_Result img_unpack_copy_request_param(elf_verify_req *req_msg)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    req_msg->version = img_info_ptr->img_version;
    if (strcpy_s(req_msg->tmp_file, sizeof(req_msg->tmp_file), img_info_ptr->tmp_file_name) != 0) {
        tloge("copy file name failed\n");
        return TEE_ERROR_BAD_PARAMETERS;
    }
    req_msg->img_size = img_info_ptr->img_size;

    return TEE_SUCCESS;
}

static void img_unpack_copy_reply_msg(const elf_verify_reply *verify_reply)
{
    elf_image_info *img_info_ptr = get_img_info_ptr();
    if (verify_reply->off_ta_elf == INVALID_OFFSET)
        img_info_ptr->ptr_ta_elf = NULL;
    else
        img_info_ptr->ptr_ta_elf = img_info_ptr->img_buf + verify_reply->off_ta_elf;

    if (verify_reply->off_manifest_buf == INVALID_OFFSET)
        img_info_ptr->ptr_manifest_buf = NULL;
    else
        img_info_ptr->ptr_manifest_buf = img_info_ptr->img_buf + verify_reply->off_manifest_buf;
}

static TEE_Result do_register_elf(const elf_verify_reply *reply, tee_img_type_t img_type)
{
    TEE_Result ret;
    elf_image_info *img_info_ptr = get_img_info_ptr();

    img_info_ptr->img_fp = open(img_info_ptr->tmp_file_name, O_RDWR, RWRIGHT, (uint64_t)0);
    if (img_info_ptr->img_fp < 0) {
        tloge("file reopen failed: %d\n", img_info_ptr->img_fp);
        return TEE_ERROR_GENERIC;
    }

    img_unpack_copy_reply_msg(reply);
    /* register img to tee */
    if (img_type == IMG_TYPE_APP || img_type == IMG_TYPE_DYNAMIC_SRV) {
        ret = load_secure_app_image(img_type, reply);
        /*
         * when load elf succ, we set tmp_file_exist to false to avoid unlink file when free_img_load_buf
         * and elf will be unlink after open session succ
         */
        if (ret == TEE_SUCCESS)
            img_info_ptr->tmp_file_exist = false;
    } else {
        ret = load_secure_lib_image(img_type, reply);
    }
    return ret;
}

static TEE_Result process_register_elf(elf_verify_reply *reply, TEE_Param *params)
{
    TEE_Result ret;
    tee_img_type_t img_type;

    /* elf verify failed, need to release dyn config */
    if (reply->verify_result != TEE_SUCCESS) {
        tloge("receive elf verify fail result 0x%x\n", reply->verify_result);
        ret = reply->verify_result;
        goto release_dyn_conf;
    }

    ret = check_verify_reply(params, reply, &img_type);
    if (ret != TEE_SUCCESS) {
        tloge("check verify reply failed\n");
        goto release_dyn_conf;
    }

    ret = do_register_elf(reply, img_type);
    if (ret != TEE_SUCCESS) {
        tloge("do register elf failed\n");
        goto release_dyn_conf;
    }

    return TEE_SUCCESS;

     /*
     * if elf verify return success, means dyn config has been registered,
     * so we should unregister dyn config when process verify reply failed
     */
release_dyn_conf:
    tee_unregister_dyn_config(reply);
    return ret;
}

static TEE_Result tee_secure_img_unpack_req(const elf_verify_req *req, const smc_cmd_t *cmd,
    TEE_Param *params, bool *async)
{
    TEE_Result ret;

#if defined(CONFIG_APP_TEE_PERM) || defined(CONFIG_APP_TEE_PERM_A32)
    /* permservice will do elf verify */
    (void)params;
    if (cmd != NULL) {
        /* backup smc cmd context here */
        if (memcpy_s(&g_load_context.smc_cmd, sizeof(g_load_context.smc_cmd), cmd, sizeof(*cmd)) != EOK) {
            tloge("backup smc cmd context failed\n");
            ret = TEE_ERROR_GENERIC;
            goto free_buf;
        }
    }
    ret = tee_ext_elf_verify_req((void *)req, sizeof(*req));
    if (ret != TEE_SUCCESS) {
        tloge("tee ext elf verify failed\n");
        goto free_buf;
    }
    g_load_context.elf_loading = true;
    /*
     * we call copy_pam_to_src to release global src here,
     * we will map again when receive ack
     */
    if (cmd != NULL)
        copy_pam_to_src(cmd->cmd_id, false);

    if (async != NULL)
        *async = true;
    /* if send req to permservice success, do not free img buf */
    return TEE_SUCCESS;
free_buf:
    free_img_load_buf();
#else
    /* if permserivce do not enable, then gtask will do elf verify */
    (void)cmd;
    (void)async;
    elf_verify_reply reply;

    ret = secure_elf_verify(req, &reply);
    reply.verify_result = ret;
    ret = process_register_elf(&reply, params);
    if (ret != TEE_SUCCESS)
        tloge("process register elf failed\n");

    free_img_load_buf();
#endif
    return ret;
}

TEE_Result load_secure_file_image(const smc_cmd_t *cmd, bool *async)
{
    TEE_Result ret;
    TEE_Param *params = NULL;
    elf_verify_req req;

    ret = load_secure_file_image_pre(cmd, &params);
    if (ret == RET_KEEP_LOADING)
        return TEE_SUCCESS;
    else if (ret != TEE_SUCCESS)
        goto free_buf;

    ret = img_unpack_copy_request_param(&req);
    if (ret != TEE_SUCCESS)
        goto free_buf;

    /* we close elf file here, so libelf_verify can open this file */
    ret = close_tmp_file();
    if (ret != TEE_SUCCESS)
        goto free_buf;

    ret = tee_secure_img_unpack_req(&req, cmd, params, async);
    if (ret != TEE_SUCCESS)
        tloge("Failed to send unpack secure image request\n");
    return ret;
free_buf:
    free_img_load_buf();
    return ret;
}

static void send_img_load_response(TEE_Result result)
{
    /* reply to tzdriver */
    set_tee_return_origin(&g_load_context.smc_cmd, TEE_ORIGIN_TEE);
    set_tee_return(&g_load_context.smc_cmd, result);
    ns_cmd_response(&g_load_context.smc_cmd);
}

static TEE_Result check_register_elf_caller(uint32_t task_id)
{
    spawn_uuid_t caller_uuid;
    TEE_UUID perm_uuid = TEE_SERVICE_PERM;

    if (getuuid(taskid_to_pid(task_id), &caller_uuid) != 0) {
        tloge("get register elf caller uuid failed\n");
        return TEE_ERROR_GENERIC;
    }

    if (TEE_MemCompare(&caller_uuid.uuid, &perm_uuid, sizeof(TEE_UUID)) == 0)
        return TEE_SUCCESS;

    return TEE_ERROR_NOT_SUPPORTED;
}

static TEE_Result restore_load_img_context(TEE_Param **params)
{
    set_load_ta_mode_global_ptr();

    /* clear loading flag */
    g_load_context.elf_loading = false;

    /* for ta from tzdriver */
    TEE_Result ret = tee_cmd_params_parse(&g_load_context.smc_cmd, params);
    if (ret != TEE_SUCCESS) {
        tloge("tee cmd params parse failed\n");
        return ret;
    }
    return TEE_SUCCESS;
}

static void get_dyn_srv_config(const elf_verify_reply *reply, struct srv_adaptor_config_t *srv_config)
{
    srv_config->task_prio = DEFAULT_TASK_PRIO - 1;
    srv_config->is_need_release_ta_res = reply->mani_ext.is_need_release_ta_res;
    srv_config->is_need_create_msg = reply->mani_ext.is_need_create_msg;
    srv_config->is_need_release_msg = reply->mani_ext.is_need_release_msg;
    srv_config->crash_callback = reply->mani_ext.crash_callback;
}

int32_t process_register_elf_req(uint32_t cmd_id, uint32_t task_id, const uint8_t *msg_buf, uint32_t msg_size)
{
    TEE_Result ret;
    elf_verify_reply reply;
    TEE_Param *params = NULL;

    (void)cmd_id;
    if (msg_buf == NULL || msg_size < sizeof (reply)) {
        tloge("register elf req failed, recv invalid msg\n");
        return GT_ERR_END_CMD;
    }

    /* check caller task_id is perm service */
    if (check_register_elf_caller(task_id) != TEE_SUCCESS)
        return GT_ERR_END_CMD;

    errno_t rc = memcpy_s(&reply, sizeof(reply), msg_buf, sizeof(reply));
    if (rc != EOK) {
        tloge("copy register elf reply buf failed\n");
        return GT_ERR_END_CMD;
    }

    ret = restore_load_img_context(&params);
    if (ret != TEE_SUCCESS) {
        tloge("restore load img context failed\n");
        tee_unregister_dyn_config(&reply);
        goto free_buf;
    }

    ret = process_register_elf(&reply, params);
    if (ret != TEE_SUCCESS) {
        tloge("process register elf failed\n");
        goto free_buf;
    }

free_buf:
    send_img_load_response(ret);
    free_img_load_buf();
    if (reply.mani_ext.target_type == SRV_TARGET_TYPE) {
        struct srv_adaptor_config_t srv_config = {0};
        get_dyn_srv_config(&reply, &srv_config);
        register_dynamic_task(&reply.srv_uuid, reply.service_name, &srv_config);
    }
    return (int)ret;
}

void elf_verify_crash_callback(void)
{
    if (g_load_context.elf_loading == true) {
        send_img_load_response(TEE_ERROR_TRUSTED_APP_LOAD_ERROR);
        free_img_load_buf();
        g_load_context.elf_loading = false;
    }
}
